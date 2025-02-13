import datetime
import fnmatch
import re
import time
import urllib
from webob import Request
from pathlib import Path
from typing import Any, Dict, Optional, List

from retrace.retrace import (STATUS, STATUS_DOWNLOADING, STATUS_FAIL,
                             STATUS_SUCCESS, TASK_DEBUG, TASK_RETRACE, TASK_RETRACE_INTERACTIVE,
                             TASK_VMCORE, TASK_VMCORE_INTERACTIVE,
                             KernelVer,
                             RetraceTask)
from retrace.config import Config
from retrace.util import (free_space,
                          ftp_close,
                          ftp_init,
                          ftp_list_dir,
                          human_readable_size,
                          parse_http_gettext,
                          response)

CONFIG = Config()

FTP_SUPPORTED_EXTENSIONS = [".tar.gz", ".tgz", ".tarz", ".tar.bz2", ".tar.xz",
                            ".tar", ".gz", ".bz2", ".xz", ".Z", ".zip"]


MANAGER_URL_PARSER = re.compile(r"^(.*/manager)(/(([^/]+)(/(__custom__|start|restart|restart_confirm|backtrace|savenotes|caseno|"
                                r"bugzillano|notify|delete(/(sure/?)?)?|results/([^/]+)/?)?)?)?)?$")

LONG_TYPES = {TASK_RETRACE: "Coredump retrace",
              TASK_DEBUG: "Coredump retrace - debug",
              TASK_VMCORE: "VMcore retrace",
              TASK_RETRACE_INTERACTIVE: "Coredump retrace - interactive",
              TASK_VMCORE_INTERACTIVE: "VMcore retrace - interactive"}


def is_local_task(taskid):
    try:
        RetraceTask(taskid)
    except Exception:
        return False

    return True

def get_status_for_task_manager(task, _=lambda x: x):
    status = _(STATUS[task.get_status()])
    if task.get_status() == STATUS_DOWNLOADING and task.has(RetraceTask.PROGRESS_FILE):
        status += " %s" % task.get(RetraceTask.PROGRESS_FILE)

    return status

def parse_start_options(task: RetraceTask, options: Dict[str, Any]):
    if "caseno" in options and options["caseno"]:
        try:
            task.set_caseno(int(options["caseno"]))
        except Exception:
            # caseno is invalid number - do nothing, it can be set later
            pass

    if "bugzillano" in options and options["bugzillano"]:
        try:
            bugzillano = list(filter(int, set(n.strip() for n in options["bugzillano"].replace(";", ",").split(","))))
            task.set_bugzillano(bugzillano)
        except Exception:
            # bugzillano is invalid number - do nothing, it can be set later
            pass

    if "notify" in options and options["notify"]:
        task.set_notify([email for email in set(n.strip() for n in options["notify"].replace(";", ",").split(",")) if email])
def get_current_kernelver(task: RetraceTask) -> str:
    if task and task.has_kernelver():
        return "value=\"%s\" " % task.get_kernelver()
    else:
        ""

def get_start_content_kernelver(task: Optional[RetraceTask] = None) -> str:
    return "      Kernel version (empty to autodetect): <input name=\"kernelver\" " \
           "type=\"text\" id=\"kernelver\" %s/> e.g. <code>2.6.32-287.el6.x86_64</code><br />" % get_current_kernelver(task)

def get_current_caseno(task: RetraceTask) -> str:
    if task and task.has_caseno():
        return "value=\"%d\" " % task.get_caseno()
    else:
        return ""

def get_start_content_caseno(task: Optional[RetraceTask] = None) -> str:
    return "      Case no.: <input name=\"caseno\" type=\"text\" id=\"caseno\" %s/><br />" % get_current_caseno(task)

def get_current_bugzillano(task: RetraceTask) -> str:
    if task and task.has_bugzillano():
        return "value=\"%s\"" % ", ".join(task.get_bugzillano())
    else:
        return ""

def get_start_content_bugzillano(task: Optional[RetraceTask] = None) -> str:
    return "      Bugzilla no.: <input name=\"bugzillano\" type=\"text\" id=\"bugzillano\" %s/><br />" % get_current_bugzillano(task)

def get_current_notify(task: RetraceTask) -> str:
    if task and task.has_notify():
        return "value=\"%s\"" % ", ".join(task.get_notify())
    else:
        return ""

def get_start_content_notify(task: Optional[RetraceTask] = None) -> str:
    return "      E-mail notification: <input name=\"notify\" type=\"text\" id=\"notify\" %s/><br />" % get_current_notify(task)

def get_start_content_verbose() -> str:
    return "      <input type=\"checkbox\" name=\"debug\" id=\"debug\" checked=\"checked\" />" \
           "Be more verbose in case of error<br />" \

def get_start_content_calc_md5() -> str:
    md5sum_enabled = ""
    if CONFIG["CalculateMd5"]:
        md5sum_enabled = "checked=\"checked\""
    return "      <input type=\"checkbox\" name=\"md5sum\" id=\"md5sum\" %s />" \
           "Calculate md5 checksum for all downloaded resources<br />" % md5sum_enabled

def get_available_table(rows: List) -> str:
    if len(rows) <= 0:
        return ""
    table_str = "<div id=\"available\"> " \
                "  <table> " \
                "    <tr> " \
                "       <th colspan=\"1\" class=\"tablename\">Available tasks</th> " \
                "    </tr>" \
                "    <tr>"  \
                "       <th class=\"taskid\">Task ID</th> " \
                "    </tr> "
    for r in rows:
        table_str += r
    table_str += "      </th> "\
                 "    </tr> " \
                 "  </table> " \
                 "</div>"
    return table_str

def get_running_table(rows: List) -> str:
    if len(rows) <= 0:
        return ""
    table_str = "<div id=\"running\"> " \
                "   <table> " \
                "       <tr>" \
                "           <th colspan=\"6\" class=\"tablename\">Running tasks</th> " \
                "       </tr>" \
                "       <tr>"  \
                "           <th class=\"taskid\">Task ID</th> " \
                "           <th class=\"caseno\">Case no.</th>" \
                "           <th class=\"bugzillano\">Bugzilla no.</th>" \
                "           <th>File(s)</th> " \
                "           <th class=\"timestamp\">Started</th>" \
                "           <th class=\"status\">Status</th>" \
                "       </tr> "
    for r in rows:
        table_str += r
    table_str += "   </table>" \
                 "</div>"
    return table_str

def get_finished_table(rows: List) -> str:
    if len(rows) <= 0:
        return ""
    table_str = "<div id=\"finished\"> " \
                "   <table> " \
                "       <tr>" \
                "           <th colspan=\"5\" class=\"tablename\">Finished tasks</th> " \
                "       </tr>" \
                "       <tr>"  \
                "           <th class=\"taskid\">Task ID</th> " \
                "           <th class=\"caseno\">Case no.</th>" \
                "           <th class=\"bugzillano\">Bugzilla no.</th>" \
                "           <th>File(s)</th> " \
                "           <th class=\"timestamp\">Finished</th>" \
                "       </tr> "
    for r in rows:
        table_str += r
    table_str += "   </table>" \
                 "</div>"
    return table_str

def application(environ, start_response):
    request = Request(environ)

    _ = parse_http_gettext("%s" % request.accept_language,
                           "%s" % request.accept_charset)

    ftpcallback = """
        <script>
        $.ajax({
          type: "GET",
          <URL>
          success: function( returnedData ) {
            $( '#available' ).html( returnedData );
            $( '#ftploading' ).hide();
            $( '#available' ).show();
          }
         });
        </script>
    """

    if not CONFIG["AllowTaskManager"]:
        return response(start_response, "403 Forbidden", _("Task manager was disabled by the server administrator"))

    match = MANAGER_URL_PARSER.match(request.path_url)
    if not match:
        return response(start_response, "404 Not Found")

    filename = match.group(4)
    if filename:
        filename = urllib.parse.unquote(match.group(4))

    space = free_space(CONFIG["SaveDir"])
    if space is None:
        return response(start_response, "500 Internal Server Error", _("Unable to obtain free space"))

    if match.group(6) and match.group(6).startswith("results") and match.group(9):
        try:
            task = RetraceTask(filename)
        except Exception:
            return response(start_response, "404 Not Found", _("There is no such task"))

        if not task.has_results(match.group(9)):
            return response(start_response, "404 Not Found", _("There is no such record"))

        return response(start_response, "200 OK", task.get_results(match.group(9)).decode('utf-8','ignore'))

    elif match.group(6) and match.group(6) == "start":
        # start
        GET = request.GET
        ftptask = False
        try:
            task = RetraceTask(filename)
        except Exception:
            if CONFIG["UseFTPTasks"]:
                ftp = ftp_init()
                files = ftp_list_dir(CONFIG["FTPDir"], ftp)
                if filename not in files:
                    ftp_close(ftp)
                    return response(start_response, "404 Not Found", _("There is no such task"))

                try:
                    size = ftp.size(filename)
                except Exception:
                    size = 0

                ftp_close(ftp)

                if space - size < (CONFIG["MinStorageLeft"] << 20):
                    return response(start_response, "507 Insufficient Storage",
                                    _("There is not enough free space on the server"))

                ftptask = True
            else:
                return response(start_response, "404 Not Found", _("There is no such task"))

        if ftptask:
            try:
                task = RetraceTask()
                task.set_managed(True)
                # ToDo: determine?
                task.set_type(TASK_VMCORE_INTERACTIVE)
                task.add_remote("FTP %s" % filename)
                task.set_url("%s/%d" % (match.group(1), task.get_taskid()))
            except Exception:
                return response(start_response, "500 Internal Server Error", _("Unable to create a new task"))

            if "vmem-check" in GET:
                task.add_remote("FTP %s" % GET["custom_vmem_url"])

        if not task.get_managed():
            return response(start_response, "403 Forbidden", _("Task does not belong to task manager"))

        parse_start_options(task, GET)
        debug = "debug" in GET
        kernelver = None
        arch = None
        if "kernelver" in GET and GET["kernelver"]:
            try:
                kernelver = KernelVer(GET["kernelver"])
                if kernelver.arch is None:
                    raise Exception
            except Exception as ex:
                return response(start_response, "403 Forbidden",
                                _("Please use VRA format for kernel version (e.g. 2.6.32-287.el6.x86_64)"))

            arch = kernelver.arch
            kernelver = str(kernelver)

        if "md5sum" in GET:
            task.set_md5sum("Enabled")

        task.start(debug=debug, kernelver=kernelver, arch=arch)

        # ugly, ugly, ugly! retrace-server-worker double-forks and needs a while to spawn
        time.sleep(2)

        return response(start_response, "303 See Other", "",
                        [("Location", "%s/%d" % (match.group(1), task.get_taskid()))])

    elif match.group(6) and match.group(6) == "savenotes":
        POST = request.POST
        try:
            task = RetraceTask(filename)
        except Exception:
            return response(start_response, "404 Not Found", _("There is no such task"))

        if "notes" in POST and POST["notes"]:
            task.set_notes(POST["notes"])

        return response(start_response, "302 Found", "", [("Location", "%s/%d" % (match.group(1), task.get_taskid()))])

    elif match.group(6) and match.group(6) == "notify":
        POST = request.POST
        try:
            task = RetraceTask(filename)
        except Exception:
            return response(start_response, "404 Not Found", _("There is no such task"))

        if "notify" in POST and POST["notify"]:
            task.set_notify([email for email in set(n.strip() for n in POST["notify"]
                                                    .replace(";", ",").split(",")) if email])

        return response(start_response, "302 Found", "", [("Location", "%s/%d" % (match.group(1), task.get_taskid()))])

    elif match.group(6) and match.group(6) == "caseno":
        POST = request.POST
        try:
            task = RetraceTask(filename)
        except Exception:
            return response(start_response, "404 Not Found", _("There is no such task"))

        if "caseno" in POST:
            if POST["caseno"]:
                try:
                    caseno = int(POST["caseno"])
                except Exception as ex:
                    return response(start_response, "404 Not Found", _("Case number must be an integer; %s" % ex))

                task.set_caseno(caseno)
            else:
                task.delete(RetraceTask.CASENO_FILE)

        return response(start_response, "302 Found", "", [("Location", "%s/%d" % (match.group(1), task.get_taskid()))])

    elif match.group(6) and match.group(6) == "bugzillano":
        POST = request.POST
        try:
            task = RetraceTask(filename)
        except Exception:
            return response(start_response, "404 Not Found", _("There is no such task"))

        if "bugzillano" in POST:
            if POST["bugzillano"]:
                try:
                    bugzillano = list(filter(int, set(n.strip() for n in POST["bugzillano"]
                                                      .replace(";", ",").split(","))))
                except ValueError as ex:
                    return response(start_response, "404 Not Found", _("Bugzilla numbers must be integers; %s" % ex))

                task.set_bugzillano(bugzillano)
            else:
                task.delete(RetraceTask.BUGZILLANO_FILE)

        return response(start_response, "302 Found", "", [("Location", "%s/%d" % (match.group(1), task.get_taskid()))])

    elif match.group(6) and match.group(6) == "backtrace":
        try:
            task = RetraceTask(filename)
        except Exception:
            return response(start_response, "404 Not Found", _("There is no such task"))

        if not task.get_managed():
            return response(start_response, "403 Forbidden", _("Task does not belong to task manager"))

        if not task.has_backtrace():
            return response(start_response, "404 Forbidden", _("There is no backtrace for the specified task"))

        return response(start_response, "200 OK", task.get_backtrace())

    elif match.group(6) and match.group(6).startswith("delete") and \
         match.group(8) and match.group(8).startswith("sure"):
        try:
            task = RetraceTask(filename)
        except Exception:
            return response(start_response, "404 Not Found", _("There is no such task"))

        if not task.get_managed():
            return response(start_response, "403 Forbidden", _("Task does not belong to task manager"))

        if CONFIG["TaskManagerAuthDelete"]:
            return response(start_response, "403 Forbidden", _("Authorization required to delete tasks"))

        task.remove()

        return response(start_response, "302 Found", "", [("Location", match.group(1))])

    elif match.group(6) and match.group(6) == "restart_confirm":
        try:
            task = RetraceTask(filename)
        except Exception:
            return response(start_response, "404 Not Found", _("There is no such task"))

        with open("/usr/share/retrace-server/managertask.xhtml", "r") as f:
            output = f.read(1 << 20) # 1MB

        title = "%s #%s - %s" % (_("Task"), task.get_taskid(), _("Retrace Server Task Manager"))
        taskno = "%s #%s" % (_("Task"), task.get_taskid())
        tasktype = _(LONG_TYPES[task.get_type()])
        status = get_status_for_task_manager(task, _=_)
        baseurl = request.path_url.replace('restart_confirm','')
        startcontent = "    <form method=\"post\" action=\"%s/restart\">" % baseurl.rstrip("/") + \
                       get_start_content_kernelver(task) + get_start_content_caseno(task) + \
                       get_start_content_bugzillano(task) + get_start_content_notify(task) + \
                       get_start_content_verbose() + \
                       "      <input type=\"submit\" value=\"%s\" id=\"start\" class=\"button\" />" \
                       "    </form>" % _("Restart task")
        start = "<tr>" \
                "  <td colspan=\"2\">" \
                "%s" \
                "  </td>" \
                "</tr>" % startcontent
        back = "<tr><td colspan=\"2\"><a href=\"%s\">%s</a></td></tr>" % (match.group(1), _("Back to task manager"))
        md5sum = ""
        if task.has_md5sum():
            md5sum = "<tr><th>Md5sum:</th><td>%s</td></tr>" % task.get_md5sum()

        output = output.replace("{title}", title)
        output = output.replace("{taskno}", taskno)
        output = output.replace("{str_type}", _("Type:"))
        output = output.replace("{type}", tasktype)
        output = output.replace("{str_status}", _("Status:"))
        output = output.replace("{status}", status)
        output = output.replace("{start}", start)
        output = output.replace("{back}", back)
        output = output.replace("{backtrace}", "")
        output = output.replace("{backtracewindow}", "")
        output = output.replace("{caseno}", "")
        output = output.replace("{bugzillano}", "")
        output = output.replace("{notify}", "")
        output = output.replace("{delete}", "")
        output = output.replace("{delete_yesno}", "")
        output = output.replace("{restart}", "")
        output = output.replace("{interactive}", "")
        output = output.replace("{results}", "")
        output = output.replace("{notes}", "")
        output = output.replace("{md5sum}", md5sum)
        output = output.replace("{unknownext}", "")
        output = output.replace("{downloaded}", "")
        output = output.replace("{starttime}", "")
        output = output.replace("{finishtime}", "")
        return response(start_response, "200 OK", output, [("Content-Type", "text/html")])

    elif match.group(6) and match.group(6) == "restart":
        POST = request.POST
        try:
            task = RetraceTask(filename)
        except Exception:
            return response(start_response, "404 Not Found", _("There is no such task"))
        if not task.has_finished_time() or (task.get_status() != STATUS_SUCCESS and task.get_status() != STATUS_FAIL):
            return response(start_response, "403 Forbidden",
                                _("Task is still running, cannot restart"))

        parse_start_options(task, POST)
        debug = "debug" in POST
        kernelver = None
        arch = None
        if "kernelver" in POST and POST["kernelver"]:
            try:
                kernelver = KernelVer(POST["kernelver"])
                if kernelver.arch is None:
                    raise Exception
            except Exception as ex:
                return response(start_response, "403 Forbidden",
                                _("Please use VRA format for kernel version (e.g. 2.6.32-287.el6.x86_64)"))

            arch = kernelver.arch
            kernelver = str(kernelver)

        task.restart(debug=debug, kernelver=kernelver, arch=arch)

        return response(start_response, "302 Found", "", [("Location", "%s/%d" % (match.group(1), task.get_taskid()))])

    elif filename and filename == "__custom__":
        POST = request.POST

        qs_base = []
        if "md5sum" in POST and POST["md5sum"] == "on":
            qs_base.append("md5sum=md5sum")

        if "debug" in POST and POST["debug"] == "on":
            qs_base.append("debug=debug")

        if "kernelver" in POST:
            kernelver = POST["kernelver"]

            if kernelver.strip():
                try:
                    kver = KernelVer(kernelver)
                    if kver.arch is None:
                        raise Exception
                except Exception:
                    return response(start_response, "403 Forbidden",
                                    _("Please use VRA format for kernel version (e.g. 2.6.32-287.el6.x86_64)"))

                qs_base.append("kernelver=%s" % urllib.parse.quote(kernelver))

        try:
            task = RetraceTask()
        except Exception as ex:
            return response(start_response, "500 Internal Server Error", _("Unable to create a new task"))

        if "task_type" in POST and POST["task_type"] == "coredump":
            task.set_type(TASK_RETRACE_INTERACTIVE)
            if "package" in POST and POST["package"]:
                task.set("custom_package", POST["package"])
            if "executable" in POST and POST["executable"]:
                task.set("custom_executable", POST["executable"])
            if "os_release" in POST and POST["os_release"]:
                task.set("custom_os_release", POST["os_release"])
        else:
            task.set_type(TASK_VMCORE_INTERACTIVE)
            if "vmem-check" in POST and POST["custom_vmem_url"]:
                task.add_remote(POST["custom_vmem_url"])

        task.add_remote(POST["custom_url"])
        task.set_managed(True)
        task.set_url("%s/%d" % (match.group(1), task.get_taskid()))

        starturl = "%s/%d/start" % (match.group(1), task.get_taskid())
        if qs_base:
            starturl = "%s?%s" % (starturl, "&".join(qs_base))

        return response(start_response, "302 Found", "", [("Location", starturl)])

    elif filename:
        # info
        ftptask = False
        filesize = None
        try:
            task = RetraceTask(filename)
        except Exception:
            if CONFIG["UseFTPTasks"]:
                ftp = ftp_init()
                files = ftp_list_dir(CONFIG["FTPDir"], ftp)
                if filename not in files:
                    ftp_close(ftp)
                    return response(start_response, "404 Not Found", _("There is no such task"))

                ftptask = True
                try:
                    filesize = ftp.size(filename)
                except Exception:
                    pass
                ftp_close(ftp)
            else:
                return response(start_response, "404 Not Found", _("There is no such task"))

        with open("/usr/share/retrace-server/managertask.xhtml", "r") as f:
            output = f.read(1 << 20) # 1MB

        start = ""
        if not ftptask and task.has_status():
            status = get_status_for_task_manager(task, _=_)
        else:
            startcontent = "    <form method=\"get\" action=\"%s/start\">" % request.path_url.rstrip("/") + \
                           get_start_content_kernelver() + get_start_content_caseno() + \
                           get_start_content_bugzillano() + get_start_content_notify() + \
                           get_start_content_verbose() + get_start_content_calc_md5() + \
                           "      <input type=\"submit\" value=\"%s\" id=\"start\" class=\"button\" />" \
                           "    </form>" % _("Start task")

            if ftptask:
                status = _("On remote FTP server")
                if filesize:
                    status += " (%s)" % human_readable_size(filesize)

                if space - filesize < (CONFIG["MinStorageLeft"] << 20):
                    startcontent = _("You can not start the task because there is not enough free space on the server")
            else:
                status = _("Not started")

            start = "<tr>" \
                    "  <td colspan=\"2\">" \
                    "%s" \
                    "  </td>" \
                    "</tr>" % startcontent

        interactive = ""
        backtrace = ""
        backtracewindow = ""
        if not ftptask:
            if task.has_backtrace():
                backtrace = "<tr><td colspan=\"2\"><a href=\"%s/backtrace\">%s</a></td></tr>" \
                            % (request.path_url.rstrip("/"), _("Show raw backtrace"))
                backtracewindow = "<h2>Backtrace</h2><textarea class=\"backtrace\">%s</textarea>" % task.get_backtrace()
                if task.get_type() in [TASK_RETRACE_INTERACTIVE, TASK_VMCORE_INTERACTIVE]:
                    if task.get_type() == TASK_VMCORE_INTERACTIVE:
                        debugger = "crash"
                    else:
                        debugger = "gdb"

                    interactive = "<tr><td colspan=\"2\">%s</td></tr>" \
                                  "<tr><td colspan=\"2\">%s <code>retrace-server-interact %s shell</code></td></tr>" \
                                  "<tr><td colspan=\"2\">%s <code>retrace-server-interact %s %s</code></td></tr>" \
                                  "<tr><td colspan=\"2\">%s <code>man retrace-server-interact</code> %s</td></tr>" \
                                  % (_("This is an interactive task"), _("You can jump to the chrooted shell with:"),
                                     filename, _("You can jump directly to the debugger with:"), filename, debugger,
                                     _("see"), _("for further information about cmdline flags"))
            elif task.has_log():
                backtracewindow = "<h2>Log:</h2><textarea class=\"backtrace\">%s</textarea>" % task.get_log()

        if ftptask or task.is_running(readproc=True) or CONFIG["TaskManagerAuthDelete"]:
            delete = ""
        else:
            delete = "<tr><td colspan=\"2\"><a href=\"%s/delete\">%s</a></td></tr>" \
                     % (request.path_url.rstrip("/"), _("Delete task"))

        if ftptask or task.is_running(readproc=True):
            restart = ""
        else:
            restart = "<tr><td colspan=\"2\"><a href=\"%s/restart_confirm\">%s</a></td></tr>" \
                     % (request.path_url.rstrip("/"), _("Restart task"))

        if ftptask:
            # ToDo: determine?
            tasktype = _(LONG_TYPES[TASK_VMCORE_INTERACTIVE])
            title = "%s '%s' - %s" % (_("Remote file"), filename, _("Retrace Server Task Manager"))
            taskno = "%s '%s'" % (_("Remote file"), filename)
        else:
            tasktype = _(LONG_TYPES[task.get_type()])
            title = "%s #%s - %s" % (_("Task"), filename, _("Retrace Server Task Manager"))
            taskno = "%s #%s" % (_("Task"), filename)

        results = ""
        if not ftptask:
            results_list = sorted(task.get_results_list())
            if results_list:
                links = []
                for name in results_list:
                    links.append("<a href=\"%s/results/%s\">%s</a>" % (request.path_url.rstrip("/"), name, name))
                results = "<tr><th>%s</th><td>%s</td></tr>" % (_("Additional results:"), ", ".join(links))

        if match.group(6) and match.group(6).startswith("delete") and not CONFIG["TaskManagerAuthDelete"]:
            delete_yesno = "<tr><td colspan=\"2\">%s <a href=\"%s/sure\">Yes</a> - <a href=\"%s/%s\">No</a></td></tr>" \
                           % (_("Are you sure you want to delete the task?"), request.path_url.rstrip("/"),
                              match.group(1), filename)
        else:
            delete_yesno = ""

        unknownext = ""
        if ftptask:
            known = any(filename.endswith(ext) for ext in FTP_SUPPORTED_EXTENSIONS)
            if not known:
                unknownext = "<tr><td colspan=\"2\">%s %s</td></tr>" % \
                             (_("The file extension was not recognized, thus the file will be "
                                "considered a raw vmcore. Known extensions are:"),
                              ", ".join(FTP_SUPPORTED_EXTENSIONS))

        downloaded = ""
        if not ftptask and task.has_downloaded():
            downloaded = "<tr><th>Downloaded resources:</th><td>%s</td></tr>" % task.get_downloaded()

        starttime_str = ""
        if not ftptask:
            if task.has_started_time():
                starttime = task.get_started_time()
            else:
                starttime = task.get_default_started_time()

            starttime_str = "<tr><th>Started:</th><td>%s</td></tr>" % datetime.datetime.fromtimestamp(starttime)

        md5sum = ""
        if not ftptask and task.has_md5sum():
            md5sum = "<tr><th>Md5sum:</th><td>%s</td></tr>" % task.get_md5sum()

        finishtime_str = ""
        if not ftptask:
            if task.has_finished_time():
                finishtime = task.get_finished_time()
            else:
                finishtime = task.get_default_finished_time()


            finishtime_str = "<tr><th>Finished:</th><td>%s</td></tr>" % datetime.datetime.fromtimestamp(finishtime)

        caseno = ""
        if not ftptask:
            caseno = "<tr>" \
                     "  <th>Case no.:</th>" \
                     "  <td>" \
                     "    <form method=\"post\" action=\"%s/caseno\">" \
                     "      <input type=\"text\" name=\"caseno\" %s/>" \
                     "      <input type=\"submit\" value=\"Update case no.\" class=\"button\" />" \
                     "    </form>" \
                     "  </td>" \
                     "</tr>" % (request.path_url.rstrip("/"), get_current_caseno(task))

        bugzillano = ""
        if not ftptask:
            bugzillano = "<tr>" \
                     "  <th>Bugzilla no.:</th>" \
                     "  <td>" \
                     "    <form method=\"post\" action=\"%s/bugzillano\">" \
                     "      <input type=\"text\" name=\"bugzillano\" %s/>" \
                     "      <input type=\"submit\" value=\"Update bugzilla no.\" class=\"button\" />" \
                     "    </form>" \
                     "  </td>" \
                     "</tr>" % (request.path_url.rstrip("/"), get_current_bugzillano(task))

        back = "<tr><td colspan=\"2\"><a href=\"%s\">%s</a></td></tr>" % (match.group(1), _("Back to task manager"))

        notes = ""
        if not ftptask:
            notes_quoted = ""
            if task.has_notes():
                notes_quoted = task.get_notes().replace("<", "&lt;") \
                                               .replace(">", "&gt;") \
                                               .replace("\"", "&quot;") \
                                               .replace("'", "&apos;")

            notes = "<form method=\"post\" action=\"%s/savenotes\" class=\"notes\">" \
                    "  <h2>Notes</h2>" \
                    "  <textarea class=\"notes\" name=\"notes\">%s</textarea>" \
                    "  <input type=\"submit\" value=\"Update notes\" class=\"button\" />" \
                    "</form>" % (request.path_url.rstrip("/"), notes_quoted)

        notify = ""
        if not ftptask:
            notify = "<tr>" \
                     "  <th>E-mail notification:</th>" \
                     "  <td>" \
                     "    <form method=\"post\" action=\"%s/notify\">" \
                     "      <input type=\"text\" name=\"notify\" %s/>" \
                     "      <input type=\"submit\" value=\"Update e-mail(s)\" class=\"button\" />" \
                     "    </form>" \
                     "  </td>" \
                     "</tr>" % (request.path_url.rstrip("/"), get_current_notify(task))

        output = output.replace("{title}", title)
        output = output.replace("{taskno}", taskno)
        output = output.replace("{str_type}", _("Type:"))
        output = output.replace("{type}", tasktype)
        output = output.replace("{str_status}", _("Status:"))
        output = output.replace("{status}", status)
        output = output.replace("{start}", start)
        output = output.replace("{back}", back)
        output = output.replace("{backtrace}", backtrace)
        output = output.replace("{backtracewindow}", backtracewindow)
        output = output.replace("{caseno}", caseno)
        output = output.replace("{bugzillano}", bugzillano)
        output = output.replace("{notify}", notify)
        output = output.replace("{delete}", delete)
        output = output.replace("{delete_yesno}", delete_yesno)
        output = output.replace("{restart}", restart)
        output = output.replace("{interactive}", interactive)
        output = output.replace("{results}", results)
        output = output.replace("{notes}", notes)
        output = output.replace("{md5sum}", md5sum)
        output = output.replace("{unknownext}", unknownext)
        output = output.replace("{downloaded}", downloaded)
        output = output.replace("{starttime}", starttime_str)
        output = output.replace("{finishtime}", finishtime_str)
        return response(start_response, "200 OK", output, [("Content-Type", "text/html")])

    # menu
    with open("/usr/share/retrace-server/manager.xhtml") as f:
        output = f.read(1 << 20) # 1MB

    title = _("Retrace Server Task Manager")

    baseurl = request.path_url
    if not baseurl.endswith("/"):
        baseurl += "/"

    try:
        filterexp = request.GET.getone("filter")
    except Exception:
        filterexp = None

    available = []
    running = []
    finished = []
    for taskdir in sorted(Path(CONFIG["SaveDir"]).iterdir()):
        if not taskdir.is_dir():
            continue

        try:
            task = RetraceTask(taskdir.name)
        except Exception:
            continue

        if not task.get_managed():
            continue

        if task.has_status():
            statuscode = task.get_status()
            if statuscode in [STATUS_SUCCESS, STATUS_FAIL]:
                status = ""
                if statuscode == STATUS_SUCCESS:
                    status = " class=\"success\""
                elif statuscode == STATUS_FAIL:
                    status = " class=\"fail\""

                finishtime = task.get_default_finished_time()
                if task.has_finished_time():
                    finishtime = task.get_finished_time()

                finishtime_str = datetime.datetime.fromtimestamp(finishtime)

                caseno = ""
                if task.has_caseno():
                    caseno = str(task.get_caseno())

                    url = CONFIG["CaseNumberURL"].strip()
                    if url:
                        try:
                            link = url % task.get_caseno()
                            caseno = "<a href=\"%s\">%d</a>" % (link, task.get_caseno())
                        except Exception:
                            pass

                bugzillano = ""
                if task.has_bugzillano():
                    bugzillano = min(task.get_bugzillano(), key=int)

                    bzurl = CONFIG["BugzillaURL"].strip()
                    if bzurl:
                        bugzillano = "<a href={0}/{1}>{1}</a>".format(bzurl, bugzillano)

                files = ""
                if task.has_downloaded():
                    files = task.get_downloaded()

                row = "<tr%s>" \
                      "  <td class=\"taskid\">" \
                      "    <a href=\"%s%s\">%s</a>" \
                      "  </td>" \
                      "  <td>%s</td>" \
                      "  <td>%s</td>" \
                      "  <td>%s</td>" \
                      "  <td>%s</td>" \
                      "</tr>" % (status, baseurl, taskdir.name, taskdir.name, caseno, bugzillano, files,
                                 finishtime_str)

                if filterexp and not fnmatch.fnmatch(row, filterexp):
                    continue

                finished.append((finishtime_str, row))
            else:
                status = get_status_for_task_manager(task, _=_)

                starttime = task.get_default_started_time()
                if task.has_started_time():
                    starttime = task.get_started_time()

                starttime_str = datetime.datetime.fromtimestamp(starttime)

                caseno = ""
                if task.has_caseno():
                    caseno = str(task.get_caseno())

                    url = CONFIG["CaseNumberURL"].strip()
                    if url:
                        try:
                            link = url % task.get_caseno()
                            caseno = "<a href=\"%s\">%d</a>" % (link, task.get_caseno())
                        except Exception:
                            pass

                bugzillano = ""
                if task.has_bugzillano():
                    bugzillano = min(task.get_bugzillano(), key=int)

                    bzurl = CONFIG["BugzillaURL"].strip()
                    if bzurl:
                        bugzillano = "<a href={0}/{1}>{1}</a>".format(bzurl, bugzillano)

                files = ""
                if task.has_remote():
                    remote = [x[4:] if x.startswith("FTP ") else x for x in task.get_remote()]
                    files = ", ".join(remote)

                if task.has_downloaded():
                    files = ", ".join(filter(None, [task.get_downloaded(), files]))

                row = "<tr>" \
                      "  <td class=\"taskid\">" \
                      "    <a href=\"%s%s\">%s</a>" \
                      "  </td>" \
                      "  <td>%s</td>" \
                      "  <td>%s</td>" \
                      "  <td>%s</td>" \
                      "  <td>%s</td>" \
                      "  <td>%s</td>" \
                      "</tr>" % (baseurl, taskdir.name, taskdir.name, caseno, bugzillano, files, starttime_str,
                                 status)

                if filterexp and not fnmatch.fnmatch(row, filterexp):
                    continue

                running.append((starttime_str, row))
        else:
            row = "<tr>" \
                  "  <td>" \
                  "    <a href=\"%s%s\">%s</a>" \
                  "  </td>" \
                  "</tr>" % (baseurl, taskdir.name, taskdir.name)

            if filterexp and not fnmatch.fnmatch(row, filterexp):
                continue

            available.append(row)

    finished = [f[1] for f in sorted(finished, key=lambda x: x[0], reverse=True)]
    running = [r[1] for r in sorted(running, key=lambda x: x[0], reverse=True)]

    taskid_str = _("Task ID")
    caseno_str = _("Case no.")
    bugzillano_str = _("Bugzilla no.")
    files_str = _("File(s)")
    starttime_str = _("Started")
    finishtime_str = _("Finished")
    status_str = _("Status")

    md5_enabled = ""
    if CONFIG["CalculateMd5"]:
        md5_enabled = 'checked="checked"'

    custom_url = "%s/__custom__" % match.group(1)

    vmcore_form = ""
    if CONFIG["AllowVMCoreTask"]:
        with open("/usr/share/retrace-server/manager_vmcore_task_form.xhtml") as f:
            vmcore_form = f.read(1 << 20) # 1MB
    output = output.replace("{vmcore_task_form}", vmcore_form)

    usrcore_form = ""
    if CONFIG["AllowUsrCoreTask"]:
        with open("/usr/share/retrace-server/manager_usrcore_task_form.xhtml") as f:
            usrcore_form = f.read(1 << 20) # 1MB
    output = output.replace("{usrcore_task_form}", usrcore_form)

    output = output.replace("{title}", title)
    output = output.replace("{available_table}", get_available_table(available))
    output = output.replace("{running_table}", get_running_table(running))
    output = output.replace("{finished_table}", get_finished_table(finished))
    output = output.replace("{create_custom_url}", custom_url)
    output = output.replace("{md5_enabled}", md5_enabled)

    return response(start_response, "200 OK", output, [("Content-Type", "text/html")])
