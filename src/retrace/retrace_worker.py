import os
import sys
import datetime
import errno
import time
import grp
import logging
import shutil
import stat
from pathlib import Path
from subprocess import PIPE, DEVNULL, STDOUT, run
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple, Union

from retrace.hooks.hooks import RetraceHook
from .retrace import (ALLOWED_FILES, EXPLOITABLE_SEPARATOR, PYTHON_LABEL_END,
                      PYTHON_LABEL_START, REPO_PREFIX, REQUIRED_FILES,
                      STATUS, STATUS_ANALYZE, STATUS_BACKTRACE, STATUS_CLEANUP,
                      STATUS_FAIL, STATUS_INIT, STATUS_STATS, STATUS_SUCCESS,
                      TASK_DEBUG, TASK_RETRACE, TASK_RETRACE_INTERACTIVE, TASK_VMCORE,
                      TASK_VMCORE_INTERACTIVE, RETRACE_GPG_KEYS, SNAPSHOT_SUFFIXES,
                      get_active_tasks,
                      get_supported_releases,
                      guess_arch,
                      is_package_known,
                      KernelVer,
                      KernelVMcore,
                      log_exception,
                      log_debug,
                      log_error,
                      log_info,
                      log_warn,
                      logger,
                      run_gdb,
                      Release,
                      RetraceTask,
                      RetraceWorkerError)
from .config import Config, PODMAN_BIN
from .plugins import Plugins
from .stats import (init_crashstats_db,
                    save_crashstats,
                    save_crashstats_build_ids,
                    save_crashstats_packages,
                    save_crashstats_success)
from .util import (INPUT_PACKAGE_PARSER,
                   human_readable_size,
                   parse_rpm_name,
                   send_email)

sys.path.insert(0, "/usr/share/retrace-server/")

CONFIG = Config()


class RetraceWorker:
    def __init__(self, task: RetraceTask):
        self.plugins = Plugins()
        self.task = task
        self.logging_handler: Optional[logging.FileHandler] = None
        self.hook = RetraceHook(task)
        self.plugin: Optional[ModuleType] = None
        self.stats: Dict[str, Any] = {}
        self.prerunning: int = 0

    def begin_logging(self) -> None:
        if self.logging_handler is None:
            self.logging_handler = logging.FileHandler(
                self.task._get_file_path(RetraceTask.LOG_FILE))

        formatter = logging.Formatter(fmt="[%(asctime)s] [%(levelname)-.1s] %(message)s",
                                      datefmt="%Y-%m-%d %H:%M:%S")
        self.logging_handler.setFormatter(formatter)
        logger.addHandler(self.logging_handler)

    def end_logging(self) -> None:
        if self.logging_handler is not None:
            logger.removeHandler(self.logging_handler)

    def notify_email(self) -> None:
        task = self.task
        if not CONFIG["EmailNotify"] or not task.has_notify():
            return
        if task.get_status() == STATUS_SUCCESS:
            disposition = "succeeded"
        else:
            disposition = "failed"

        try:
            log_info("Sending e-mail to %s" % ", ".join(task.get_notify()))

            message = "The task #%d started on %s %s\n\n" % (task.get_taskid(), os.uname()[1], disposition)

            if task.has_url():
                message += "URL: %s\n" % task.get_url()

            message += "Task directory: %s\n" % task.get_savedir()

            if task.has_started_time():
                started_time = task.get_started_time()
                assert isinstance(started_time, int)
                message += "Started: %s\n" % datetime.datetime.fromtimestamp(started_time)

            if task.has_finished_time():
                finished_time = task.get_finished_time()
                assert isinstance(finished_time, int)
                message += "Finished: %s\n" % datetime.datetime.fromtimestamp(finished_time)

            if task.has_md5sum():
                message += "MD5sum: %s" % task.get_md5sum()

            if task.has_kernelver():
                message += "Kernelver: %s\n" % task.get_kernelver()

            if task.has_remote() or task.has_downloaded():
                files = ""
                if task.has_remote():
                    remote = [x[4:] if x.startswith("FTP ") else x for x in task.get_remote()]
                    files = ", ".join(remote)

                if task.has_downloaded():
                    files = ", ".join(filter(None, [task.get_downloaded(), files]))

                message += "Remote file(s): %s\n" % files

            if task.get_type() in [TASK_VMCORE, TASK_VMCORE_INTERACTIVE] and task.get_status() == STATUS_FAIL:
                message += "\nIf kernel version detection failed (the log shows 'Unable to determine kernel " \
                           "version'), and you know the kernel version, you may try re-starting the task " \
                           "with the 'retrace-server-task restart' command.  Please check the log below " \
                           "for more information on why the task failed.  The following example assumes " \
                           "the vmcore's kernel version is 2.6.32-358.el6 on x86_64 arch: \n" \
                           "$ retrace-server-task restart --kernelver 2.6.32-358.el6.x86_64 %d\n" \
                           % task.get_taskid()
                message += "\nIf this is a test kernel with a non-errata kernel version, or for some reason " \
                           "the kernel-debuginfo repository is unavailable, you can place the kernel-debuginfo RPM " \
                           "at %s/download/ and restart the task with: \n$ retrace-server-task restart %d\n" \
                           % (CONFIG["RepoDir"], task.get_taskid())
                message += "\nIf the retrace-log contains a message similar to 'Failing task due to crash " \
                           "exiting with non-zero status and small kernellog size' then the vmcore may be " \
                           "truncated or incomplete and not useable.  Check the md5sum on the manager page " \
                           "and compare with the expected value, and possibly re-upload and resubmit the vmcore.\n"

            if task.has_log():
                message += "\nLog:\n%s\n" % task.get_log()

            send_email("Retrace Server <%s>" % CONFIG["EmailNotifyFrom"],
                       task.get_notify(),
                       "Retrace Task #%d on %s %s" % (task.get_taskid(), os.uname()[1], disposition),
                       message)

        except Exception as ex:
            log_error("Failed to send e-mail: %s" % ex)

    def _symlink_log(self) -> None:
        if self.task.has_log():
            # add a symlink to log to results directory
            # use underscore so that the log is first in the list
            try:
                (self.task.get_results_dir() / "retrace-log").symlink_to(
                    self.task._get_file_path(RetraceTask.LOG_FILE))
            except OSError as ex:
                if ex.errno != errno.EEXIST:
                    raise

    def _fail(self, errorcode: int = 1) -> None:
        task = self.task
        task.set_status(STATUS_FAIL)
        task.set_finished_time(int(time.time()))
        self.notify_email()

        self._symlink_log()

        starttime = self.stats["starttime"]
        # starttime is assigned a value in start() and never changed.
        assert starttime is not None
        self.stats["duration"] = int(time.time()) - starttime
        try:
            save_crashstats(self.stats)
        except Exception as ex:
            log_warn("Failed to save crash statistics: %s" % str(ex))

        if not task.get_type() in [TASK_DEBUG, TASK_RETRACE_INTERACTIVE, TASK_VMCORE_INTERACTIVE]:
            self.clean_task()

        self.hook.run("fail")

        raise RetraceWorkerError(errorcode=errorcode)

    def _retrace_run(self, errorcode: int, cmd: List[str]) -> str:
        "Runs cmd using subprocess.Popen and kills script with errorcode on failure"
        try:
            child = run(cmd, stdout=PIPE, stderr=STDOUT, encoding='utf-8', check=False)
            output = child.stdout
        except Exception as ex:
            child = None
            log_error("An unhandled exception occured: %s" % ex)

        if not child or child.returncode != 0:
            exit_code = str(child.returncode) if child else 'unknown'
            log_error("%s exited with %s\n=== OUTPUT ===\n%s" % (" ".join(cmd), exit_code, output))
            self._fail(errorcode)

        return output

    @staticmethod
    def _check_required_file(req: str, crashdir: Path) -> bool:
        path = Path(crashdir, req)

        if path.is_file():
            return True

        if path.name == "vmcore":
            for suffix in SNAPSHOT_SUFFIXES:
                if path.with_suffix(suffix).is_file():
                    return True

        return False

    def guess_release(self, package: str, plugins) -> Union[Tuple[None, None], Tuple[str, str]]:
        for plugin in plugins:
            match = plugin.guessparser.search(package)
            if match:
                self.plugin = plugin
                return plugin.distribution, match.group(1)

        return None, None

    def read_architecture(self, custom_arch: Optional[str], corepath: Path) -> str:
        if custom_arch is not None:
            log_debug("Using custom architecture: %s" % custom_arch)
            return custom_arch

        # read architecture from coredump
        arch = guess_arch(corepath)

        if arch is None:
            log_error("Unable to determine architecture from coredump")
            self._fail()

        log_debug("Determined architecture: %s" % arch)

        assert isinstance(arch, str)
        return arch

    def read_package_file(self, crashdir: Path) -> Tuple[str, Dict[str, Any]]:
        # read package file
        try:
            with Path(crashdir, "package").open() as package_file:
                crash_package = package_file.read(ALLOWED_FILES["package"])
        except Exception as ex:
            log_error("Unable to read crash package from 'package' file: %s" % ex)
            self._fail()
        # read package file
        if not INPUT_PACKAGE_PARSER.match(crash_package):
            log_error("Invalid package name: %s" % crash_package)
            self._fail()

        pkgdata = parse_rpm_name(crash_package)
        if not pkgdata["name"]:
            log_error("Unable to parse package name: %s" % crash_package)
            self._fail()
        return (crash_package, pkgdata)

    def read_release_file(self, crashdir: Path, arch: str,
                          crash_package: Optional[str] = None) -> Release:
        # read release, distribution and version from release file
        release_path = None
        rootdir = None
        rootdir_path = crashdir / "rootdir"
        if rootdir_path.is_file():
            with rootdir_path.open() as rootdir_file:
                rootdir = rootdir_file.read(ALLOWED_FILES["rootdir"])

            exec_path = crashdir / "executable"
            with exec_path.open() as exec_file:
                executable = exec_file.read(ALLOWED_FILES["executable"])

            if executable.startswith(rootdir):
                with exec_path.open('w') as exec_file:
                    exec_file.write(executable[len(rootdir):])

            rel_path = crashdir / "os_release_in_rootdir"
            if rel_path.is_file():
                release_path = rel_path

        if not release_path:
            release_path = crashdir / "os_release"
            if not release_path.is_file():
                release_path = crashdir / "release"

        release_name = "Unknown Release"
        try:
            with release_path.open() as release_file:
                release_name = release_file.read(ALLOWED_FILES["os_release"])

            version = distribution = None
            for plugin in self.plugins.all():
                match = plugin.abrtparser.match(release_name)
                if match:
                    version = match.group(1)
                    distribution = plugin.distribution
                    self.plugin = plugin
                    break

            if not version or not distribution:
                raise Exception("Unknown release '%s'" % release_name)

        except Exception as ex:
            log_error("Unable to read distribution and version from 'release' file: %s" % ex)
            if crash_package is None:
                log_error("Cannot guess distribution and version without package")
                self._fail()
            assert crash_package is not None

            log_info("Trying to guess distribution and version")
            distribution, version = self.guess_release(crash_package, self.plugins.all())
            if distribution and version:
                log_info("%s-%s" % (distribution, version))
            else:
                log_error("Failure")
                self._fail()

        # Checked above.
        assert distribution is not None
        assert version is not None

        release = Release(distribution, version, arch, release_name)

        if "rawhide" in release_name.lower():
            release.is_rawhide = True

        return release

    def read_packages(self, crashdir: Path, releaseid: str, crash_package: str,
                      distribution: str) -> Tuple[List[str], List[Tuple[str, str]]]:
        packages = [crash_package]
        missing = []

        packagesfile = crashdir / "packages"
        if packagesfile.is_file():
            with packagesfile.open() as f:
                packages = f.read().split()
        else:
            # read required packages from coredump
            try:
                repoid = "%s%s" % (REPO_PREFIX, releaseid)
                dnfcfgpath = self.task.get_savedir() / "dnf.conf"
                with dnfcfgpath.open("w") as dnfcfg:
                    dnfcfg.write("[%s]\n" % repoid)
                    dnfcfg.write("name=%s\n" % releaseid)
                    dnfcfg.write("baseurl=file://%s/%s/\n" % (CONFIG["RepoDir"], releaseid))
                    dnfcfg.write("failovermethod=priority\n")

                child = run(["coredump2packages",
                             str(crashdir / "coredump"),
                             f"--repos={repoid}",
                             f"--config={dnfcfgpath}",
                             "--log=%s" % (self.task.get_savedir() / "c2p_log")],
                            stdout=PIPE, stderr=PIPE, encoding='utf-8', check=False)
                section = 0
                lines = child.stdout.split("\n")
                libdb = False
                for line in lines:
                    if line == "":
                        section += 1
                        continue
                    if section == 1:
                        stripped = line.strip()

                        # hack - help to depsolver, yum would fail otherwise
                        # unknow for DNF
                        if distribution == "fedora" and stripped.startswith("gnome"):
                            packages.append("desktop-backgrounds-gnome")

                        # hack - libdb-debuginfo and db4-debuginfo are conflicting
                        if distribution == "fedora" and \
                           (stripped.startswith("db4-debuginfo") or stripped.startswith("libdb-debuginfo")):
                            if libdb:
                                continue
                            libdb = True

                        packages.append(stripped)
                    elif section == 2:
                        soname, buildid = line.strip().split(" ", 1)
                        if not soname or soname == "-":
                            soname = None
                        missing.append((soname, buildid))

                if child.stderr:
                    log_warn(child.stderr)

            except Exception as ex:
                log_error("Unable to obtain packages from 'coredump' file: %s" % ex)
                self._fail()
        return (packages, missing)

    def construct_gpg_keys(self, version: str, pre_rawhide_version: Optional[int],
                           scheme: str = "file://") -> str:
        final_result = ""

        for key in self.plugin.gpg_keys:
            gpg_key = key.format(release=version)
            final_result += f"{scheme}{gpg_key} "

        if pre_rawhide_version and self.plugin.gpg_keys:
            gpg_key = self.plugin.gpg_keys[0].format(release=pre_rawhide_version)
            final_result += f"{scheme}{gpg_key} "

        return final_result

    def ensure_image_exists(self, release: Release, repopath: str, gpg_keys: str) \
            -> str:
        """
        Ensure that a container image exists for retracing coredumps
        from the specified release. The image comes preinstalled with
        GDB and abrt-addon-ccpp and a script to run GDB and collects
        its output.

        Return the tag of the container image.
        """

        image_tag = f"localhost/retrace-image:{release}"

        # Check if the image exists first.
        child = run([PODMAN_BIN, "image", "inspect", image_tag],
                    stdout=DEVNULL, stderr=DEVNULL, check=False)

        if child.returncode == 0:
            log_info(f"Corresponding container image {image_tag} exists")
            return image_tag

        # Since the image does not exist, create the Containerfile and
        # other required files in a temporary directory, and run Podman
        # to build and tag the image.
        with TemporaryDirectory() as tempdir_path:
            log_info(f"Created directory {tempdir_path} to build image for "
                     f"retracing {release}")
            tempdir = Path(tempdir_path)

            # Create the DNF repository file.
            with (tempdir / "retrace-podman.repo").open("w") as repofile:
                repofile.write(f"[retrace-{release.distribution}]\n"
                               f"name=Retrace Server repository for {release}\n"
                               f"baseurl=file://{repopath}/\n"
                               f"gpgcheck={CONFIG['RequireGPGCheck']}\n"
                               f"gpgkey={gpg_keys}\n")

            # Create batch script for running GDB commands.
            with (tempdir / "gdb.sh").open("w") as gdbfile:
                gdb_exec = self.plugin.gdb_executable
                gdbfile.write("#!/bin/sh\n"
                              f"{gdb_exec} -batch \\\n"
                              "  -ex 'python exec(open(\"/usr/libexec/abrt-gdb-exploitable\").read())' \\\n")

                if not self.task.get_debuginfod_enabled():
                    gdbfile.write("  -ex 'file '$1 \\\n")

                gdbfile.write("  -ex 'core-file /var/spool/abrt/crash/coredump' \\\n"
                              f"  -ex 'echo {PYTHON_LABEL_START}\\n' \\\n"
                              "  -ex 'py-bt' \\\n"
                              "  -ex 'py-list' \\\n"
                              "  -ex 'py-locals' \\\n"
                              f"  -ex 'echo {PYTHON_LABEL_END}\\n' \\\n"
                              "  -ex 'thread apply all -ascending backtrace full 2048' \\\n"
                              "  -ex 'info sharedlib' \\\n"
                              "  -ex 'print (char*)__abort_msg' \\\n"
                              "  -ex 'print (char*)__glib_assert_msg' \\\n"
                              "  -ex 'info registers' \\\n"
                              "  -ex 'disassemble' \\\n"
                              f"  -ex 'echo {EXPLOITABLE_SEPARATOR}\\n' \\\n"
                              "  -ex 'abrt-exploitable'")

            os.chmod(tempdir / "gdb.sh", 0o755)

            # Create Containerfile with instructions for building the image.
            with (tempdir / "Containerfile").open("w") as cntfile:
                rpm_import_command = "true"
                if CONFIG["RequireGPGCheck"]:
                    rpm_import_command = f"rpm --import {gpg_keys}"

                dnf_flags = " ".join(["--assumeyes",
                                      "--setopt=tsflags=nodocs",
                                      f"--releasever={release.version}",
                                      f"--repo=retrace-{release.distribution}"])

                cntfile.write(f"FROM {release.distribution}:{release.version}\n\n"
                              f"RUN useradd --no-create-home --no-log-init retrace && \\\n"
                              f"    mkdir --parents /var/spool/abrt/crash/\n"
                              "COPY retrace-podman.repo /etc/yum.repos.d/\n"
                              "COPY --chown=retrace gdb.sh /var/spool/abrt/\n\n"
                              f"RUN {rpm_import_command} && \\\n"
                              f"    dnf {dnf_flags} \\\n"
                              f"        install abrt-addon-ccpp {self.plugin.gdb_package} && \\\n"
                              f"    dnf clean all")

            # Build the image.
            build_call = [PODMAN_BIN, "build",
                          "--quiet",
                          "--force-rm",
                          "--file", str(tempdir / "Containerfile"),
                          "--volume", f"{repopath}:{repopath}:ro",
                          "--tag", image_tag]

            if CONFIG["RequireGPGCheck"]:
                build_call.append("--volume={0}:{0}:ro".format(RETRACE_GPG_KEYS))

            if CONFIG["UseFafPackages"]:
                log_debug("Using FAF repository")
                build_call.append("--volume={0}:{0}:ro".format(CONFIG["FafLinkDir"]))

            child = run(build_call, stdout=sys.stderr, stderr=PIPE, encoding='utf-8',
                        check=False)

            if child.returncode:
                raise Exception("Could not build container image. Podman exited "
                                f"with code {child.returncode}: {child.stderr}")

        log_info(f"Container image {image_tag} successfully created")

        return image_tag

    def start_retrace(self, custom_arch: Optional[str] = None) -> bool:
        self.hook.run("start")

        task = self.task
        savedir = task.get_savedir()
        crashdir = task.get_crashdir()
        corepath = crashdir / RetraceTask.COREDUMP_FILE
        debuginfod_enabled = self.task.get_debuginfod_enabled()
        try:
            self.stats["coresize"] = corepath.stat().st_size
        except OSError:
            pass

        arch = self.read_architecture(custom_arch, corepath)
        self.stats["arch"] = arch

        crash_package, pkgdata = self.read_package_file(crashdir)
        self.stats["package"] = pkgdata["name"]
        if pkgdata["epoch"] is not None and pkgdata["epoch"] != 0:
            self.stats["version"] = "%d:%s-%s" % (pkgdata["epoch"], pkgdata["version"], pkgdata["release"])
        else:
            self.stats["version"] = "%s-%s" % (pkgdata["version"], pkgdata["release"])

        pre_rawhide_version = None
        release = self.read_release_file(crashdir, arch, crash_package)

        if release.is_rawhide:
            # some packages might not be signed by rawhide key yet
            # but with a key of previous release
            pre_rawhide_version = int(release.version) - 1
            release.version = "rawhide"

        releaseid = str(release)
        if not debuginfod_enabled and releaseid not in get_supported_releases():
            log_error("Release ‘%s’ is not supported" % releaseid)
            self._fail()

        if not debuginfod_enabled and not is_package_known(crash_package, arch, releaseid):
            log_error("Package ‘%s.%s’ was not recognized.\nIs it a part of "
                      "official %s repositories?" % (crash_package, arch, release.name))
            self._fail()
        self.hook.run("pre_prepare_debuginfo")

        if not debuginfod_enabled:
            packages, missing = self.read_packages(crashdir, releaseid, crash_package, release.distribution)
        else:
            packages, missing = ([], [])

        self.hook.run("post_prepare_debuginfo")
        self.hook.run("pre_prepare_environment")

        repopath = os.path.join(CONFIG["RepoDir"], releaseid)
        gpg_keys_string = self.construct_gpg_keys(release.version, pre_rawhide_version)

        if CONFIG["RetraceEnvironment"] == "mock":
            # create mock config file
            try:
                with (savedir / RetraceTask.MOCK_DEFAULT_CFG).open("w") as mockcfg:
                    mockcfg.write("config_opts['root'] = '%d'\n" % task.get_taskid())
                    mockcfg.write("config_opts['target_arch'] = '%s'\n" % arch)
                    mockcfg.write("config_opts['chroot_setup_cmd'] = '")
                    mockcfg.write(" install %s abrt-addon-ccpp shadow-utils %s rpm'\n" % (" ".join(packages),
                                                                                          self.plugin.gdb_package))
                    mockcfg.write("config_opts['releasever'] = '%s'\n" % release.version)
                    mockcfg.write("config_opts['package_manager'] = 'dnf'\n")
                    mockcfg.write("config_opts['plugin_conf']['ccache_enable'] = False\n")
                    mockcfg.write("config_opts['plugin_conf']['yum_cache_enable'] = False\n")
                    mockcfg.write("config_opts['plugin_conf']['root_cache_enable'] = False\n")
                    mockcfg.write("config_opts['plugin_conf']['bind_mount_enable'] = True\n")
                    mockcfg.write("config_opts['plugin_conf']['bind_mount_opts'] = { 'create_dirs': True,\n")
                    mockcfg.write("    'dirs': [\n")
                    mockcfg.write("              ('%s', '%s'),\n" % (repopath, repopath))
                    if CONFIG["RequireGPGCheck"]:
                        mockcfg.write("              ('%s', '%s'),\n" % (RETRACE_GPG_KEYS, RETRACE_GPG_KEYS))
                    mockcfg.write("              ('%s', '/var/spool/abrt/crash'),\n" % crashdir)
                    mockcfg.write("            ] }\n")
                    mockcfg.write("\n")
                    mockcfg.write("config_opts['yum.conf'] = \"\"\"\n")
                    mockcfg.write("[main]\n")
                    mockcfg.write("cachedir=/var/cache/yum\n")
                    mockcfg.write("debuglevel=1\n")
                    mockcfg.write("reposdir=%s\n" % os.devnull)
                    mockcfg.write("logfile=/var/log/yum.log\n")
                    mockcfg.write("retries=20\n")
                    mockcfg.write("obsoletes=1\n")
                    mockcfg.write("gpgcheck=%s\n" % CONFIG["RequireGPGCheck"])
                    mockcfg.write("assumeyes=1\n")
                    mockcfg.write("syslog_ident=mock\n")
                    mockcfg.write("syslog_device=\n")
                    mockcfg.write("\n")
                    mockcfg.write("#repos\n")
                    mockcfg.write("\n")
                    mockcfg.write("[%s]\n" % release.distribution)
                    mockcfg.write("name=%s\n" % releaseid)
                    mockcfg.write("baseurl=file://%s/\n" % repopath)
                    mockcfg.write("gpgkey=%s\n" % gpg_keys_string)
                    mockcfg.write("failovermethod=priority\n")
                    mockcfg.write("\"\"\"\n")

                # symlink defaults from /etc/mock
                (savedir / RetraceTask.MOCK_SITE_DEFAULTS_CFG).symlink_to(
                    "/etc/mock/site-defaults.cfg")
                (savedir / RetraceTask.MOCK_LOGGING_INI).symlink_to("/etc/mock/logging.ini")
            except Exception as ex:
                log_error("Unable to create mock config file: %s" % ex)
                self._fail()

        # run retrace
        task.set_status(STATUS_INIT)
        log_info(STATUS[STATUS_INIT])

        if CONFIG["RetraceEnvironment"] == "mock":
            self._retrace_run(25, ["/usr/bin/mock", "init", "--resultdir",
                                   str(savedir / "log"), "--configdir",
                                   str(savedir)])

            self.hook.run("post_prepare_environment")
            self.hook.run("pre_retrace")

            self._retrace_run(27, ["/usr/bin/mock", "--configdir", str(savedir), "chroot",
                                   "--", "chgrp -R mock /var/spool/abrt/crash"])
            image_tag = ""
        elif CONFIG["RetraceEnvironment"] == "podman":
            try:
                image_tag = self.ensure_image_exists(release, repopath, gpg_keys_string)
            except Exception as ex:
                log_error("Could not ensure container image exists: %s" % ex)
                raise

            self.hook.run("post_prepare_environment")
            self.hook.run("pre_retrace")

        # generate backtrace
        task.set_status(STATUS_BACKTRACE)
        log_info(STATUS[STATUS_BACKTRACE])

        try:
            backtrace, exploitable = run_gdb(savedir, repopath, task.get_taskid(),
                                             image_tag, packages, corepath, release,
                                             debuginfod_enabled, self.plugin)
        except Exception as ex:
            log_error("Could not run GDB: %s" % ex)
            self._fail()

        task.set_backtrace(backtrace)
        if exploitable is not None:
            task.add_results("exploitable", exploitable, mode="w")

        self.hook.run("post_retrace")

        # does not work at the moment
        rootsize = 0

        if not task.get_type() in [TASK_DEBUG, TASK_RETRACE_INTERACTIVE]:
            # clean up temporary data
            task.set_status(STATUS_CLEANUP)
            log_info(STATUS[STATUS_CLEANUP])

            self.clean_task()

        # save crash statistics
        task.set_status(STATUS_STATS)
        log_info(STATUS[STATUS_STATS])

        task.set_finished_time(int(time.time()))
        starttime = self.stats["starttime"]
        # starttime is assigned a value in start() and never changed.
        assert starttime is not None
        self.stats["duration"] = int(time.time()) - starttime
        self.stats["status"] = STATUS_SUCCESS

        try:
            con = init_crashstats_db()
            statsid = save_crashstats(self.stats, con)
            save_crashstats_success(statsid, self.prerunning, len(get_active_tasks()),
                                    rootsize, con)
            save_crashstats_packages(statsid, packages[1:], con)
            if missing:
                save_crashstats_build_ids(statsid, missing, con)
            con.close()
        except Exception as ex:
            log_warn(str(ex))

        # publish log => finish task
        log_info("Retrace took %d seconds" % self.stats["duration"])

        log_info(STATUS[STATUS_SUCCESS])
        task.set_status(STATUS_SUCCESS)

        self.hook.run("success")

        return True

    # de-dup self.task's vmcore with md5_tasks's vmcore
    def dedup_vmcore(self, md5_task):
        task1 = md5_task   # primary
        task2 = self.task  # one we are going to try to hardlink and the one that gets logged to
        v1 = task1.get_vmcore_path()
        v2 = task2.get_vmcore_path()
        try:
            s1 = v1.stat()
            s2 = v2.stat()
        except OSError:
            log_warn("Attempt to dedup %s and %s but 'stat' failed on one of the paths" % (v1, v2))
            return 0

        if s1.st_ino == s2.st_ino:
            return 0
        if s1.st_size != s2.st_size:
            log_warn("Attempt to dedup %s and %s but sizes differ - size1 = %d size2 = %d"
                     % (v1, v2, s1.st_size, s2.st_size))
            return 0
        v1_md5 = str.split(task1.get_md5sum())[0]
        v2_md5 = str.split(task2.get_md5sum())[0]
        if len(v1_md5) != 32 or len(v2_md5) != 32:
            return 0
        if v1_md5 != v2_md5:
            log_warn("Attempted to dedup %s and %s but md5sums are different - v1 = %s v2 = %s)"
                     % (v1, v2, v1_md5, v2_md5))
            return 0

        v2_link = v2.parent / (v2.name + "-link")
        try:
            os.link(v1, v2_link)
        except OSError:
            log_warn("Failed to dedup %s and %s - failed to create hard link from %s to %s" % (v1, v2, v2_link, v1))
            return 0
        try:
            v2.unlink()
        except OSError:
            log_warn("Failed to dedup %s and %s - unlink of %s failed" % (v1, v2, v2))
            os.unlink(v2_link)
            return 0
        try:
            v2_link.rename(v2)
        except OSError:
            log_error("ERROR: Failed to dedup %s and %s - rename hardlink %s to %s failed" % (v1, v2, v2_link, v2))
            return 0

        log_warn("Successful dedup - created hardlink from %s to %s saving %d MB"
                 % (v2, v1, s1.st_size // 1024 // 1024))

        return s1.st_size

    def start_vmcore(self, custom_kernelver: Optional[KernelVer] = None) -> None:
        self.hook.run("start")

        task = self.task
        crashdir = task.get_crashdir()
        task.find_vmcore_file()
        if not task.get_vmcore_path().is_file():
            raise Exception("Vmcore file not found at %s" % task.get_vmcore_path())
        vmcore_path = task.get_vmcore_path()
        log_debug("Task vmcore path: %s" % task.get_vmcore_path())

        try:
            self.stats["coresize"] = vmcore_path.stat().st_size
        except OSError:
            pass

        vmcore = KernelVMcore(vmcore_path)
        oldsize = vmcore_path.stat().st_size
        log_info("Vmcore size: %s" % human_readable_size(oldsize))
        if vmcore.is_flattened_format():
            start = time.time()
            log_info("Executing makedumpfile to convert flattened format")
            # NOTE: We do not need to know the kernelver or vmlinux path here
            vmcore.convert_flattened_format()
            dur = int(time.time() - start)
            newsize = vmcore_path.stat().st_size
            log_info("Converted size: %s" % human_readable_size(newsize))
            log_info("Makedumpfile took %d seconds and saved %s"
                     % (dur, human_readable_size(oldsize - newsize)))
            oldsize = newsize

        if custom_kernelver is not None:
            kernelver = custom_kernelver
        else:
            crash_cmd = task.get_crash_cmd()
            assert crash_cmd is not None
            kernelver = vmcore.get_kernel_release(crash_cmd.split())
            if kernelver is None:
                raise Exception("Unable to determine kernel version")

            log_debug("Determined kernel version: %s" % kernelver)

        task.set_kernelver(kernelver)
        kernelver_str = kernelver.kernelver_str

        self.stats["package"] = "kernel"
        self.stats["version"] = kernelver_str
        self.stats["arch"] = kernelver.arch

        log_info(STATUS[STATUS_INIT])
        task.set_status(STATUS_INIT)
        vmlinux = ""

        if CONFIG["RetraceEnvironment"] == "mock":
            self.hook.run("post_prepare_environment")

            # we don't save config into task.get_savedir() because it is only
            # readable by user/group retrace/CONFIG["AuthGroup"].
            # if a non-retrace user in group mock executes
            # setgid /usr/bin/mock, he gets permission denied.
            # this is not a security thing - using mock gives you root anyway
            cfgdir = Path(CONFIG["SaveDir"], "%d-kernel" % task.get_taskid())

            # if the directory exists, it is orphaned - nuke it
            if cfgdir.is_dir():
                shutil.rmtree(cfgdir)

            mockgid = grp.getgrnam("mock").gr_gid
            old_umask = os.umask(0o027)
            cfgdir.mkdir()
            os.chown(cfgdir, -1, mockgid)

            try:
                cfgfile = cfgdir / RetraceTask.MOCK_DEFAULT_CFG
                with cfgfile.open("w") as mockcfg:
                    mockcfg.write("config_opts['root'] = '%d-kernel'\n" % task.get_taskid())
                    mockcfg.write("config_opts['target_arch'] = '%s'\n" % kernelver.arch)
                    mockcfg.write("config_opts['chroot_setup_cmd'] = 'install bash coreutils cpio "
                                  "crash findutils rpm shadow-utils'\n")
                    mockcfg.write("config_opts['releasever'] = '%s'\n" % kernelver_str)
                    mockcfg.write("config_opts['package_manager'] = 'dnf'\n")
                    mockcfg.write("config_opts['plugin_conf']['ccache_enable'] = False\n")
                    mockcfg.write("config_opts['plugin_conf']['yum_cache_enable'] = False\n")
                    mockcfg.write("config_opts['plugin_conf']['root_cache_enable'] = False\n")
                    mockcfg.write("config_opts['plugin_conf']['bind_mount_enable'] = True\n")
                    mockcfg.write("config_opts['plugin_conf']['bind_mount_opts'] = { \n")
                    mockcfg.write("    'dirs': [('%s', '%s'),\n" % (CONFIG["RepoDir"], CONFIG["RepoDir"]))
                    mockcfg.write("             ('%s', '%s'),],\n" % (task.get_savedir(), task.get_savedir()))
                    mockcfg.write("    'create_dirs': True, }\n")
                    mockcfg.write("\n")
                    mockcfg.write("config_opts['yum.conf'] = \"\"\"\n")
                    mockcfg.write("[main]\n")
                    mockcfg.write("cachedir=/var/cache/yum\n")
                    mockcfg.write("debuglevel=1\n")
                    mockcfg.write("reposdir=%s\n" % os.devnull)
                    mockcfg.write("logfile=/var/log/yum.log\n")
                    mockcfg.write("retries=20\n")
                    mockcfg.write("obsoletes=1\n")
                    mockcfg.write("assumeyes=1\n")
                    mockcfg.write("syslog_ident=mock\n")
                    mockcfg.write("syslog_device=\n")
                    mockcfg.write("\n")
                    mockcfg.write("#repos\n")
                    mockcfg.write("\n")
                    mockcfg.write("[kernel-%s]\n" % kernelver.arch)
                    mockcfg.write("name=kernel-%s\n" % kernelver.arch)
                    mockcfg.write("baseurl=%s\n" % CONFIG["KernelChrootRepo"].replace("$ARCH", kernelver.arch))
                    mockcfg.write("failovermethod=priority\n")
                    mockcfg.write("\"\"\"\n")

                os.chown(cfgfile, -1, mockgid)

                # symlink defaults from /etc/mock
                (task.get_savedir() / RetraceTask.MOCK_SITE_DEFAULTS_CFG).symlink_to(
                    "/etc/mock/site-defaults.cfg")
                (task.get_savedir() / RetraceTask.MOCK_LOGGING_INI).symlink_to("/etc/mock/logging.ini")
            except Exception as ex:
                raise Exception("Unable to create mock config file: %s" % ex)
            finally:
                os.umask(old_umask)

            child = run(["/usr/bin/mock", "--configdir", str(cfgdir), "init"],
                        stdout=PIPE, stderr=PIPE, encoding='utf-8', check=False)
            stderr = child.stderr
            if child.returncode:
                raise Exception("mock exited with %d:\n%s" % (child.returncode, stderr))

            self.hook.run("post_prepare_environment")

            # no locks required, mock locks itself
            try:
                self.hook.run("pre_prepare_debuginfo")
                vmlinux = vmcore.prepare_debuginfo(task, cfgdir, kernelver=kernelver)
                self.hook.run("post_prepare_debuginfo")
            except Exception as ex:
                raise Exception("prepare_debuginfo failed: %s" % str(ex))

            self.hook.run("pre_retrace")
            crash_cmd = task.get_crash_cmd()
            assert crash_cmd is not None
            crash_normal = ["/usr/bin/mock", "--configdir", str(cfgdir), "--cwd", str(crashdir),
                            "chroot", "--", crash_cmd + " -s %s %s" % (vmcore_path, vmlinux)]
            crash_minimal = ["/usr/bin/mock", "--configdir", str(cfgdir), "--cwd", str(crashdir),
                             "chroot", "--", crash_cmd + " -s --minimal %s %s" % (vmcore_path, vmlinux)]

        elif CONFIG["RetraceEnvironment"] == "podman":
            savedir = task.get_savedir()

            # Guess OS release from kernel release.
            distribution, version = self.guess_release(kernelver.release,
                                                       self.plugins.all())
            if distribution is None:
                raise Exception("Could not guess OS release from kernel release "
                                f"‘{kernelver.release}’")

            assert distribution is not None
            assert version is not None

            try:
                with (savedir / RetraceTask.CONTAINERFILE).open("w") as cntfile:
                    cntfile.write(f"FROM {distribution}:{version}\n\n")
                    cntfile.write("RUN dnf "
                                  f"--releasever={version} "
                                  "--assumeyes "
                                  "--skip-broken "
                                  "install bash coreutils cpio crash findutils rpm "
                                  "shadow-utils && dnf clean all\n")
                    cntfile.write("RUN dnf "
                                  "--assumeyes "
                                  "--enablerepo=*debuginfo* "
                                  "install kernel-debuginfo\n\n")
                    cntfile.write("RUN useradd --no-create-home --no-log-init retrace\n")
                    cntfile.write("RUN mkdir --parents /var/spool/abrt/crash\n\n")
                    cntfile.write("COPY --chown=retrace crash/{} /var/spool/abrt/crash/\n\n"
                                  .format(vmcore_path.name))
                    cntfile.write("USER retrace\n\n")
                    cntfile.write("CMD [\"/usr/bin/bash\"]")
            except Exception as ex:
                log_error("Unable to create Containerfile: %s" % ex)
                self._fail()

            taskid = task.get_taskid()

            child = run([PODMAN_BIN,
                         "build",
                         "--file={}".format(savedir / RetraceTask.CONTAINERFILE),
                         f"--tag=retrace-image:{taskid}"],
                        stdout=PIPE, stderr=STDOUT, encoding="utf-8", check=False)
            if child.returncode:
                raise Exception(f"Unable to build podman container: {child.stdout}")

            vmlinux = vmcore.prepare_debuginfo(task, kernelver=kernelver)
            child = run([PODMAN_BIN, "run",
                         "--quiet",
                         "--detach",
                         "--interactive",
                         "--tty",
                         "--sdnotify=ignore",
                         "--rm",
                         f"--name=retrace-{taskid}",
                         f"retrace-image:{taskid}"],
                        stdout=PIPE, stderr=PIPE, encoding="utf-8", check=False)
            if child.stderr:
                log_error(child.stderr)
                raise Exception("Unable to run podman container")

            crash_normal = [PODMAN_BIN, "exec", f"retrace-{taskid}",
                            task.get_crash_cmd(),
                            "-s",
                            f"/var/spool/abrt/crash/{vmcore_path.name}",
                            vmlinux]
            crash_minimal = [PODMAN_BIN, "exec", f"retrace-{taskid}",
                             task.get_crash_cmd(),
                             "-s",
                             "--minimal",
                             f"/var/spool/abrt/crash/{vmcore_path.name}",
                             vmlinux]
        elif CONFIG["RetraceEnvironment"] == "native":
            try:
                self.hook.run("pre_prepare_debuginfo")
                vmlinux = vmcore.prepare_debuginfo(task, kernelver=kernelver)
                self.hook.run("post_prepare_debuginfo")
            except Exception as ex:
                raise Exception("prepare_debuginfo failed: %s" % str(ex))

            self.hook.run("pre_retrace")
            task.set_status(STATUS_BACKTRACE)
            log_info(STATUS[STATUS_BACKTRACE])

            crash_cmd = task.get_crash_cmd()
            assert isinstance(crash_cmd, str)
            crash_normal = crash_cmd.split() + ["-s", str(vmcore_path), vmlinux]
            crash_minimal = crash_cmd.split() + ["--minimal", "-s", str(vmcore_path), vmlinux]

        else:
            raise Exception("RetraceEnvironment set to invalid value")

        if vmcore.has_extra_pages(task):
            log_info("Executing makedumpfile to strip extra pages")
            start = time.time()
            # NOTE: We need to know the kernelver and vmlinux path here
            vmcore.strip_extra_pages()
            dur = int(time.time() - start)
            newsize = vmcore_path.stat().st_size
            log_info("Stripped size: %s" % human_readable_size(newsize))
            log_info("Makedumpfile took %d seconds and saved %s"
                     % (dur, human_readable_size(oldsize - newsize)))

        if vmcore_path.is_file():
            st = vmcore_path.stat()
            if (st.st_mode & stat.S_IRGRP) == 0:
                try:
                    vmcore_path.chmod(st.st_mode | stat.S_IRGRP)
                except Exception:
                    log_warn("File '%s' is not group readable and chmod"
                             " failed. The process will continue but if"
                             " it fails this is the likely cause."
                             % vmcore_path)

        # Generate the kernel log and run other crash commands
        kernellog, ret = task.run_crash_cmdline(crash_minimal, "log\nquit\n")

        if CONFIG["RetraceEnvironment"] == "podman":
            # TODO: Remove container and image.
            pass

        task.set_backtrace(kernellog, "wb")
        # If crash sys command exited with non-zero status,
        # we likely have a semi-useful vmcore
        crash_sys, ret = task.run_crash_cmdline(crash_normal, "sys\nquit\n")

        if ret == 0 and crash_sys:
            task.add_results("sys", crash_sys)
        else:
            # FIXME: Probably a better hueristic can be done here
            if len(kernellog) < 1024:
                # If log < 1024 bytes, probably it is not useful so fail task
                raise Exception("Failing task due to crash exiting with non-zero status and "
                                "small kernellog size = %d bytes" % len(kernellog))
            # If log is 1024 bytes or above, try 'crash --minimal'
            task.set_crash_cmd(task.get_crash_cmd() + " --minimal")

        crashrc_lines = []

        if "/" in vmlinux:
            crashrc_lines.append("mod -S %s > %s" % (vmlinux.rsplit("/", 1)[0], os.devnull))

        results_dir = task.get_results_dir()
        crashrc_lines.append("cd %s" % results_dir)

        if crashrc_lines:
            task.set_crashrc("%s\n" % "\n".join(crashrc_lines))

        self.hook.run("post_retrace")

        task.set_finished_time(int(time.time()))
        self.stats["duration"] = int(time.time()) - self.stats["starttime"]
        self.stats["status"] = STATUS_SUCCESS

        log_info(STATUS[STATUS_STATS])

        try:
            save_crashstats(self.stats)
        except Exception as ex:
            log_error(str(ex))

        # clean up temporary data
        task.set_status(STATUS_CLEANUP)
        log_info(STATUS[STATUS_CLEANUP])

        if not task.get_type() in [TASK_VMCORE_INTERACTIVE]:
            self.clean_task()

        log_info("Retrace took %d seconds" % self.stats["duration"])
        log_info(STATUS[STATUS_SUCCESS])

        self._symlink_log()

        task.set_status(STATUS_SUCCESS)
        self.notify_email()
        self.hook.run("success")

    def start(self, kernelver: Optional[KernelVer] = None, arch: Optional[str] = None) -> None:
        self.hook.run("pre_start")
        self.stats = {
            "taskid": self.task.get_taskid(),
            "package": None,
            "version": None,
            "arch": None,
            "starttime": int(time.time()),
            "duration": None,
            "coresize": None,
            "status": STATUS_FAIL,
        }
        self.prerunning = len(get_active_tasks()) - 1
        try:
            task = self.task

            task.set_started_time(int(time.time()))

            if task.has_remote():
                errors = task.download_remote()
                if errors:
                    for _, error in errors:
                        log_warn(error)

            task.set_status(STATUS_ANALYZE)
            log_info(STATUS[STATUS_ANALYZE])

            crashdir = task.get_crashdir()

            tasktype = task.get_type()

            if task.has("custom_executable"):
                shutil.copyfile(task._get_file_path("custom_executable"),
                                crashdir / "executable")
            if task.has("custom_package"):
                shutil.copyfile(task._get_file_path("custom_package"),
                                crashdir / "package")
            if task.has("custom_os_release"):
                shutil.copyfile(task._get_file_path("custom_os_release"),
                                crashdir / "os_release")

            for required_file in REQUIRED_FILES[tasktype]:
                if not self._check_required_file(required_file, crashdir):
                    raise Exception("Crash directory does not contain required file '%s'" % required_file)

            if tasktype in [TASK_RETRACE, TASK_DEBUG, TASK_RETRACE_INTERACTIVE]:
                self.start_retrace(custom_arch=arch)
            elif tasktype in [TASK_VMCORE, TASK_VMCORE_INTERACTIVE]:
                self.start_vmcore(custom_kernelver=kernelver)
            else:
                raise Exception("Unsupported task type '%s'" % tasktype)
        except Exception as ex:
            log_error("Task failed: %s" % ex)
            log_exception("Backtrace: %s" % ex)
            self._fail()

    def clean_task(self) -> None:
        self.hook.run("pre_clean_task")
        self.task.clean()
        self.hook.run("post_clean_task")

    def remove_task(self):
        self.hook.run("pre_remove_task")
        self.task.remove()
        self.hook.run("post_remove_task")
