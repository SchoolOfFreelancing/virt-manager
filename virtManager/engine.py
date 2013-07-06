#
# Copyright (C) 2006 Red Hat, Inc.
# Copyright (C) 2006 Daniel P. Berrange <berrange@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301 USA.
#

# pylint: disable=E0611
from gi.repository import GLib
from gi.repository import GObject
from gi.repository import Gtk
# pylint: enable=E0611

import logging
import threading
import os

import libvirt
import virtinst

from virtManager import packageutils
from virtManager import uihelpers
from virtManager import util
from virtManager.about import vmmAbout
from virtManager.baseclass import vmmGObject
from virtManager.clone import vmmCloneVM
from virtManager.connect import vmmConnect
from virtManager.connection import vmmConnection
from virtManager.preferences import vmmPreferences
from virtManager.manager import vmmManager
from virtManager.migrate import vmmMigrateDialog
from virtManager.details import vmmDetails
from virtManager.asyncjob import vmmAsyncJob
from virtManager.create import vmmCreate
from virtManager.host import vmmHost
from virtManager.error import vmmErrorDialog
from virtManager.systray import vmmSystray
from virtManager.delete import vmmDeleteDialog

# Enable this to get a report of leaked objects on app shutdown
# gtk3/pygobject has issues here as of Fedora 18
debug_ref_leaks = False


DETAILS_PERF = 1
DETAILS_CONFIG = 2
DETAILS_CONSOLE = 3


class vmmEngine(vmmGObject):
    __gsignals__ = {
        "conn-added": (GObject.SignalFlags.RUN_FIRST, None, [object]),
        "conn-removed": (GObject.SignalFlags.RUN_FIRST, None, [str]),
    }

    def __init__(self):
        vmmGObject.__init__(self)

        self.windowConnect = None
        self.windowPreferences = None
        self.windowAbout = None
        self.windowCreate = None
        self.windowManager = None
        self.windowMigrate = None

        self.conns = {}
        self.err = vmmErrorDialog()

        self.timer = None
        self.last_timeout = 0

        self.systray = None
        self.delete_dialog = None
        self.application = Gtk.Application(
                                 application_id="com.redhat.virt-manager",
                                 flags=0)
        self.application.connect("activate", self._activate)
        self._appwindow = Gtk.Window()

        self._tick_thread = None
        self._tick_thread_slow = False

        self.inspection = None
        self._create_inspection_thread()

        # Counter keeping track of how many manager and details windows
        # are open. When it is decremented to 0, close the app or
        # keep running in system tray if enabled
        self.windows = 0

        # Public bits set by virt-manager cli
        self.skip_autostart = False
        self.uri_at_startup = None
        self.uri_cb = None
        self.show_manager_window = True

        self.init_systray()

        self.add_gconf_handle(
            self.config.on_stats_update_interval_changed(self.reschedule_timer))
        self.add_gconf_handle(
            self.config.on_view_system_tray_changed(self.system_tray_changed))

        self.schedule_timer()
        self.load_stored_uris()
        self.tick()


    def _activate(self, ignore):
        if self.show_manager_window:
            self.show_manager()
        else:
            self.get_manager()
        self.application.add_window(self._appwindow)

        if self.uri_at_startup:
            conn = self.add_conn_to_ui(self.uri_at_startup)
            if conn and self.uri_cb:
                conn.connect_opt_out("state-changed", self.uri_cb)

            self.connect_to_uri(self.uri_at_startup)

        if not self.skip_autostart:
            self.autostart_conns()


    def init_systray(self):
        if self.systray:
            return

        self.systray = vmmSystray(self)
        self.systray.connect("action-toggle-manager", self._do_toggle_manager)
        self.systray.connect("action-suspend-domain", self._do_suspend_domain)
        self.systray.connect("action-resume-domain", self._do_resume_domain)
        self.systray.connect("action-run-domain", self._do_run_domain)
        self.systray.connect("action-shutdown-domain", self._do_shutdown_domain)
        self.systray.connect("action-reboot-domain", self._do_reboot_domain)
        self.systray.connect("action-destroy-domain", self._do_destroy_domain)
        self.systray.connect("action-reset-domain", self._do_reset_domain)
        self.systray.connect("action-show-vm", self._do_show_vm)
        self.systray.connect("action-exit-app", self.exit_app)

    def system_tray_changed(self, *ignore):
        systray_enabled = self.config.get_view_system_tray()
        if self.windows == 0 and not systray_enabled:
            # Show the manager so that the user can control the application
            self.show_manager()

    def add_default_conn(self, manager):
        # Only add default if no connections are currently known
        if self.config.get_conn_uris():
            return

        # Manager fail message
        msg = _("Could not detect a default hypervisor. Make\n"
                "sure the appropriate virtualization packages\n"
                "are installed (kvm, qemu, libvirt, etc.), and\n"
                "that libvirtd is running.\n\n"
                "A hypervisor connection can be manually\n"
                "added via File->Add Connection")

        logging.debug("Determining default libvirt URI")

        ret = None
        try:
            libvirt_packages = self.config.libvirt_packages
            packages = self.config.hv_packages + libvirt_packages

            ret = packageutils.check_packagekit(manager.err, packages, True)
        except:
            logging.exception("Error talking to PackageKit")

        if ret is not None:
            tryuri = "qemu:///system"
        else:
            tryuri = util.default_uri(always_system=True)

        if tryuri is None:
            manager.set_startup_error(msg)
            return

        warnmsg = _("The 'libvirtd' service will need to be started.\n\n"
                    "After that, virt-manager will connect to libvirt on\n"
                    "the next application start up.")

        # Do the initial connection in an idle callback, so the
        # packagekit async dialog has a chance to go away
        def idle_connect():
            do_start = packageutils.start_libvirtd()
            if not do_start:
                manager.err.ok(_("Libvirt service must be started"), warnmsg)

            self.connect_to_uri(tryuri, autoconnect=True, do_start=do_start)
        self.idle_add(idle_connect)


    def load_stored_uris(self):
        uris = self.config.get_conn_uris()
        if not uris:
            return
        logging.debug("About to connect to uris %s", uris)
        for uri in uris:
            self.add_conn_to_ui(uri)

    def autostart_conns(self):
        for uri in self.conns:
            conn = self.conns[uri]["conn"]
            if conn.get_autoconnect():
                self.connect_to_uri(uri)


    def _do_vm_removed(self, conn, vmuuid):
        hvuri = conn.get_uri()
        if vmuuid not in self.conns[hvuri]["windowDetails"]:
            return

        self.conns[hvuri]["windowDetails"][vmuuid].cleanup()
        del(self.conns[hvuri]["windowDetails"][vmuuid])

    def _do_conn_changed(self, conn):
        if (conn.get_state() == conn.STATE_ACTIVE or
            conn.get_state() == conn.STATE_CONNECTING):
            return

        hvuri = conn.get_uri()

        for vmuuid in self.conns[hvuri]["windowDetails"].keys():
            self.conns[hvuri]["windowDetails"][vmuuid].cleanup()
            del(self.conns[hvuri]["windowDetails"][vmuuid])

        if (self.windowCreate and
            self.windowCreate.conn and
            self.windowCreate.conn.get_uri() == hvuri):
            self.windowCreate.close()

    def reschedule_timer(self, *args, **kwargs):
        ignore = args
        ignore = kwargs
        self.schedule_timer()

    def schedule_timer(self):
        interval = self.config.get_stats_update_interval() * 1000

        if self.timer is not None:
            self.remove_gobject_timeout(self.timer)
            self.timer = None

        self.timer = self.timeout_add(interval, self.tick)

    def tick(self):
        if self._tick_thread and self._tick_thread.isAlive():
            if not self._tick_thread_slow:
                logging.debug("Tick is slow, not running at requested rate.")
                self._tick_thread_slow = True
            return 1

        self._tick_thread = threading.Thread(name="Tick thread",
                                            target=self._tick, args=())
        self._tick_thread.daemon = True
        self._tick_thread.start()
        return 1

    def _tick(self):
        for uri in self.conns.keys():
            conn = self.conns[uri]["conn"]
            try:
                conn.tick()
            except KeyboardInterrupt:
                raise
            except libvirt.libvirtError, e:
                from_remote = getattr(libvirt, "VIR_FROM_REMOTE", None)
                from_rpc = getattr(libvirt, "VIR_FROM_RPC", None)
                sys_error = getattr(libvirt, "VIR_ERR_SYSTEM_ERROR", None)

                dom = e.get_error_domain()
                code = e.get_error_code()

                if (dom in [from_remote, from_rpc] and
                    code in [sys_error]):
                    logging.exception("Could not refresh connection %s", uri)
                    logging.debug("Closing connection since libvirtd "
                                  "appears to have stopped")
                else:
                    error_msg = _("Error polling connection '%s': %s") \
                        % (conn.get_uri(), e)
                    self.idle_add(lambda: self.err.show_err(error_msg))

                self.idle_add(conn.close)

        return 1

    def increment_window_counter(self, src):
        ignore = src
        self.windows += 1
        logging.debug("window counter incremented to %s", self.windows)

    def decrement_window_counter(self, src):
        self.windows -= 1
        logging.debug("window counter decremented to %s", self.windows)

        # Don't exit if system tray is enabled
        if (self.windows <= 0 and
            self.systray and
            not self.systray.is_visible()):
            # Defer this to an idle callback, since we can race with
            # a vmmDetails window being deleted.
            self.idle_add(self.exit_app, src)

    def _cleanup(self):
        uihelpers.cleanup()
        self.err = None

        if self.inspection:
            self.inspection.cleanup()
            self.inspection = None

        if self.timer is not None:
            GLib.source_remove(self.timer)

        if self.systray:
            self.systray.cleanup()
            self.systray = None

        self.get_manager()
        if self.windowManager:
            self.windowManager.cleanup()
            self.windowManager = None

        if self.windowPreferences:
            self.windowPreferences.cleanup()
            self.windowPreferences = None

        if self.windowAbout:
            self.windowAbout.cleanup()
            self.windowAbout = None

        if self.windowConnect:
            self.windowConnect.cleanup()
            self.windowConnect = None

        if self.windowCreate:
            self.windowCreate.cleanup()
            self.windowCreate = None

        if self.windowMigrate:
            self.windowMigrate.cleanup()
            self.windowMigrate = None

        if self.delete_dialog:
            self.delete_dialog.cleanup()
            self.delete_dialog = None

        # Do this last, so any manually 'disconnected' signals
        # take precedence over cleanup signal removal
        for uri in self.conns:
            self.cleanup_conn(uri)
        self.conns = {}

    def exit_app(self, src):
        if self.err is None:
            # Already in cleanup
            return

        self.cleanup()

        if debug_ref_leaks:
            objs = self.config.get_objects()

            # Engine will always appear to leak
            objs.remove(self.object_key)

            if src.object_key in objs:
                # UI that initiates the app exit will always appear to leak
                objs.remove(src.object_key)

            for name in objs:
                logging.debug("Leaked %s", name)

        logging.debug("Exiting app normally.")

        # We need this if there are any asyncdialog fobjs running
        if Gtk.main_level():
            logging.debug("%s other gtk main loops running, killing them.",
                          Gtk.main_level())
            for ignore in range(Gtk.main_level()):
                Gtk.main_quit()

        self.application.remove_window(self._appwindow)

    def _create_inspection_thread(self):
        logging.debug("libguestfs inspection support: %s",
                      self.config.support_inspection)
        if not self.config.support_inspection:
            return

        from virtManager.inspection import vmmInspection
        self.inspection = vmmInspection()
        self.inspection.start()
        self.connect("conn-added", self.inspection.conn_added)
        self.connect("conn-removed", self.inspection.conn_removed)
        return


    def add_conn_to_ui(self, uri):
        # Public method called from virt-manager.py
        conn = self._check_conn(uri)
        if conn:
            return conn

        conn = vmmConnection(uri)
        self.conns[uri] = {
            "conn": conn,
            "windowHost": None,
            "windowDetails": {},
            "windowClone": None,
        }

        conn.connect("vm-removed", self._do_vm_removed)
        conn.connect("state-changed", self._do_conn_changed)
        conn.tick()
        self.emit("conn-added", conn)

        return conn


    def connect_to_uri(self, uri, autoconnect=None, do_start=True):
        try:
            conn = self.add_conn_to_ui(uri)

            if conn.get_uri() not in (self.config.get_conn_uris() or []):
                self.config.add_conn(conn.get_uri())

            if autoconnect is not None:
                conn.set_autoconnect(bool(autoconnect))

            if do_start:
                conn.open()
            return conn
        except Exception:
            logging.exception("Error connecting to %s", uri)
            return None


    def cleanup_conn(self, uri):
        try:
            if self.conns[uri]["windowHost"]:
                self.conns[uri]["windowHost"].cleanup()
            if self.conns[uri]["windowClone"]:
                self.conns[uri]["windowClone"].cleanup()

            details = self.conns[uri]["windowDetails"]
            for win in details.values():
                win.cleanup()

            self.conns[uri]["conn"].cleanup()
        except:
            logging.exception("Error cleaning up conn in engine")


    def remove_conn(self, src, uri):
        ignore = src
        self.cleanup_conn(uri)
        del(self.conns[uri])

        self.emit("conn-removed", uri)
        self.config.remove_conn(uri)

    def connect(self, name, callback, *args):
        handle_id = vmmGObject.connect(self, name, callback, *args)

        if name == "conn-added":
            for uri in self.conns.keys():
                self.emit("conn-added",
                          self.conns[uri]["conn"])

        return handle_id

    def _check_conn(self, uri):
        conn = self.conns.get(uri)
        if conn:
            return conn["conn"]
        return None

    def _lookup_conn(self, uri):
        conn = self._check_conn(uri)
        if not conn:
            raise RuntimeError(_("Unknown connection URI %s") % uri)
        return conn

    ####################
    # Dialog launchers #
    ####################

    def _do_show_about(self, src):
        try:
            if self.windowAbout is None:
                self.windowAbout = vmmAbout()
            self.windowAbout.show()
        except Exception, e:
            src.err.show_err(_("Error launching 'About' dialog: %s") % str(e))

    def _get_preferences(self):
        if self.windowPreferences:
            return self.windowPreferences

        obj = vmmPreferences()
        self.windowPreferences = obj
        return self.windowPreferences

    def _do_show_preferences(self, src):
        try:
            self._get_preferences().show(src.topwin)
        except Exception, e:
            src.err.show_err(_("Error launching preferences: %s") % str(e))

    def _get_host_dialog(self, uri):
        if self.conns[uri]["windowHost"]:
            return self.conns[uri]["windowHost"]

        con = self._lookup_conn(uri)
        obj = vmmHost(con)

        obj.connect("action-exit-app", self.exit_app)
        obj.connect("action-view-manager", self._do_show_manager)
        obj.connect("action-restore-domain", self._do_restore_domain)
        obj.connect("host-opened", self.increment_window_counter)
        obj.connect("host-closed", self.decrement_window_counter)

        self.conns[uri]["windowHost"] = obj
        return self.conns[uri]["windowHost"]

    def _do_show_host(self, src, uri):
        try:
            self._get_host_dialog(uri).show()
        except Exception, e:
            src.err.show_err(_("Error launching host dialog: %s") % str(e))


    def _get_connect_dialog(self):
        if self.windowConnect:
            return self.windowConnect

        def completed(src, uri, autoconnect):
            ignore = src
            return self.connect_to_uri(uri, autoconnect)

        def cancelled(src):
            if len(self.conns.keys()) == 0:
                self.exit_app(src)

        obj = vmmConnect()
        obj.connect("completed", completed)
        obj.connect("cancelled", cancelled)
        self.windowConnect = obj
        return self.windowConnect


    def _do_show_connect(self, src):
        try:
            self._get_connect_dialog().show(src.topwin)
        except Exception, e:
            src.err.show_err(_("Error launching connect dialog: %s") % str(e))

    def _get_details_dialog(self, uri, uuid):
        if uuid in self.conns[uri]["windowDetails"]:
            return self.conns[uri]["windowDetails"][uuid]

        con = self._lookup_conn(uri)

        obj = vmmDetails(con.get_vm(uuid))
        obj.connect("action-save-domain", self._do_save_domain)
        obj.connect("action-destroy-domain", self._do_destroy_domain)
        obj.connect("action-reset-domain", self._do_reset_domain)
        obj.connect("action-suspend-domain", self._do_suspend_domain)
        obj.connect("action-resume-domain", self._do_resume_domain)
        obj.connect("action-run-domain", self._do_run_domain)
        obj.connect("action-shutdown-domain", self._do_shutdown_domain)
        obj.connect("action-reboot-domain", self._do_reboot_domain)
        obj.connect("action-exit-app", self.exit_app)
        obj.connect("action-view-manager", self._do_show_manager)
        obj.connect("action-migrate-domain", self._do_show_migrate)
        obj.connect("action-delete-domain", self._do_delete_domain)
        obj.connect("action-clone-domain", self._do_show_clone)
        obj.connect("details-opened", self.increment_window_counter)
        obj.connect("details-closed", self.decrement_window_counter)

        self.conns[uri]["windowDetails"][uuid] = obj
        return self.conns[uri]["windowDetails"][uuid]

    def _show_vm_helper(self, src, uri, uuid, page=None, forcepage=False):
        try:
            details = self._get_details_dialog(uri, uuid)

            if forcepage or not details.is_visible():
                if page == DETAILS_PERF:
                    details.activate_performance_page()
                elif page == DETAILS_CONFIG:
                    details.activate_config_page()
                elif page == DETAILS_CONSOLE:
                    details.activate_console_page()
                elif page is None:
                    details.activate_default_page()

            details.show()
            return details
        except Exception, e:
            src.err.show_err(_("Error launching details: %s") % str(e))

    def _do_show_vm(self, src, uri, uuid):
        self._show_vm_helper(src, uri, uuid)

    def get_manager(self):
        if self.windowManager:
            return self.windowManager

        obj = vmmManager()
        obj.connect("action-suspend-domain", self._do_suspend_domain)
        obj.connect("action-resume-domain", self._do_resume_domain)
        obj.connect("action-run-domain", self._do_run_domain)
        obj.connect("action-shutdown-domain", self._do_shutdown_domain)
        obj.connect("action-reboot-domain", self._do_reboot_domain)
        obj.connect("action-destroy-domain", self._do_destroy_domain)
        obj.connect("action-reset-domain", self._do_reset_domain)
        obj.connect("action-save-domain", self._do_save_domain)
        obj.connect("action-migrate-domain", self._do_show_migrate)
        obj.connect("action-delete-domain", self._do_delete_domain)
        obj.connect("action-clone-domain", self._do_show_clone)
        obj.connect("action-show-vm", self._do_show_vm)
        obj.connect("action-show-preferences", self._do_show_preferences)
        obj.connect("action-show-create", self._do_show_create)
        obj.connect("action-show-about", self._do_show_about)
        obj.connect("action-show-host", self._do_show_host)
        obj.connect("action-show-connect", self._do_show_connect)
        obj.connect("action-exit-app", self.exit_app)
        obj.connect("manager-opened", self.increment_window_counter)
        obj.connect("manager-closed", self.decrement_window_counter)
        obj.connect("remove-conn", self.remove_conn)
        obj.connect("add-default-conn", self.add_default_conn)

        self.connect("conn-added", obj.add_conn)
        self.connect("conn-removed", obj.remove_conn)

        self.windowManager = obj
        return self.windowManager

    def _do_toggle_manager(self, ignore):
        manager = self.get_manager()
        if manager.is_visible():
            manager.close()
        else:
            manager.show()

    def _do_show_manager(self, src):
        try:
            manager = self.get_manager()
            manager.show()
        except Exception, e:
            if not src:
                raise
            src.err.show_err(_("Error launching manager: %s") % str(e))

    def _get_create_dialog(self):
        if self.windowCreate:
            return self.windowCreate

        obj = vmmCreate(self)
        obj.connect("action-show-vm", self._do_show_vm)
        self.windowCreate = obj
        return self.windowCreate

    def _do_show_create(self, src, uri):
        try:
            self._get_create_dialog().show(src.topwin, uri)
        except Exception, e:
            src.err.show_err(_("Error launching manager: %s") % str(e))

    def _do_show_migrate(self, src, uri, uuid):
        try:
            conn = self._lookup_conn(uri)
            vm = conn.get_vm(uuid)

            if not self.windowMigrate:
                self.windowMigrate = vmmMigrateDialog(vm, self)

            self.windowMigrate.set_state(vm)
            self.windowMigrate.show(src.topwin)
        except Exception, e:
            src.err.show_err(_("Error launching migrate dialog: %s") % str(e))

    def _do_show_clone(self, src, uri, uuid):
        con = self._lookup_conn(uri)
        orig_vm = con.get_vm(uuid)
        clone_window = self.conns[uri]["windowClone"]

        try:
            if clone_window is None:
                clone_window = vmmCloneVM(orig_vm)
                self.conns[uri]["windowClone"] = clone_window
            else:
                clone_window.set_orig_vm(orig_vm)

            clone_window.show(src.topwin)
        except Exception, e:
            src.err.show_err(_("Error setting clone parameters: %s") % str(e))

    ##########################################
    # Window launchers from virt-manager cli #
    ##########################################

    def show_manager(self):
        self._do_show_manager(None)

    def show_connect(self):
        self._do_show_connect(self.get_manager())

    def show_host_summary(self, uri):
        self._do_show_host(self.get_manager(), uri)

    def show_domain_creator(self, uri):
        self._do_show_create(self.get_manager(), uri)

    def show_domain_console(self, uri, uuid):
        self._show_vm_helper(self.get_manager(), uri, uuid,
                             page=DETAILS_CONSOLE, forcepage=True)

    def show_domain_editor(self, uri, uuid):
        self._show_vm_helper(self.get_manager(), uri, uuid,
                             page=DETAILS_CONFIG, forcepage=True)

    def show_domain_performance(self, uri, uuid):
        self._show_vm_helper(self.get_manager(), uri, uuid,
                             page=DETAILS_PERF, forcepage=True)

    #######################################
    # Domain actions run/destroy/save ... #
    #######################################

    def _do_save_domain(self, src, uri, uuid):
        conn = self._lookup_conn(uri)
        vm = conn.get_vm(uuid)
        managed = bool(vm.managedsave_supported)

        if not managed and conn.is_remote():
            src.err.val_err(_("Saving virtual machines over remote "
                              "connections is not supported with this "
                              "libvirt version or hypervisor."))
            return

        if not util.chkbox_helper(src, self.config.get_confirm_poweroff,
            self.config.set_confirm_poweroff,
            text1=_("Are you sure you want to save '%s'?" % vm.get_name())):
            return

        path = None
        if not managed:
            path = util.browse_local(src.topwin,
                                     _("Save Virtual Machine"),
                                     conn,
                                     dialog_type=Gtk.FileChooserAction.SAVE,
                                     browse_reason=self.config.CONFIG_DIR_SAVE)
            if not path:
                return

        _cancel_cb = None
        if vm.getjobinfo_supported:
            _cancel_cb = (self._save_cancel, vm)

        def cb(asyncjob):
            vm.save(path, meter=asyncjob.get_meter())

        progWin = vmmAsyncJob(cb, [],
                    _("Saving Virtual Machine"),
                    _("Saving virtual machine memory to disk "),
                    src.topwin, cancel_cb=_cancel_cb)
        error, details = progWin.run()

        if error is not None:
            error = _("Error saving domain: %s") % error
            src.err.show_err(error, details=details)

    def _save_cancel(self, asyncjob, vm):
        logging.debug("Cancelling save job")
        if not vm:
            return

        try:
            vm.abort_job()
        except Exception, e:
            logging.exception("Error cancelling save job")
            asyncjob.show_warning(_("Error cancelling save job: %s") % str(e))
            return

        asyncjob.job_canceled = True
        return

    def _do_restore_domain(self, src, uri):
        conn = self._lookup_conn(uri)
        if conn.is_remote():
            src.err.val_err(_("Restoring virtual machines over remote "
                              "connections is not yet supported"))
            return

        path = util.browse_local(src.topwin,
                                 _("Restore Virtual Machine"),
                                 conn,
                                 browse_reason=self.config.CONFIG_DIR_RESTORE)

        if not path:
            return

        vmmAsyncJob.simple_async_noshow(conn.restore, [path], src,
                                        _("Error restoring domain"))

    def _do_destroy_domain(self, src, uri, uuid):
        conn = self._lookup_conn(uri)
        vm = conn.get_vm(uuid)

        if not util.chkbox_helper(src, self.config.get_confirm_forcepoweroff,
            self.config.set_confirm_forcepoweroff,
            text1=_("Are you sure you want to force poweroff '%s'?" %
                    vm.get_name()),
            text2=_("This will immediately poweroff the VM without "
                    "shutting down the OS and may cause data loss.")):
            return

        logging.debug("Destroying vm '%s'", vm.get_name())
        vmmAsyncJob.simple_async_noshow(vm.destroy, [], src,
                                        _("Error shutting down domain"))

    def _do_suspend_domain(self, src, uri, uuid):
        conn = self._lookup_conn(uri)
        vm = conn.get_vm(uuid)

        if not util.chkbox_helper(src, self.config.get_confirm_pause,
            self.config.set_confirm_pause,
            text1=_("Are you sure you want to pause '%s'?" %
                    vm.get_name())):
            return

        logging.debug("Pausing vm '%s'", vm.get_name())
        vmmAsyncJob.simple_async_noshow(vm.suspend, [], src,
                                        _("Error pausing domain"))

    def _do_resume_domain(self, src, uri, uuid):
        conn = self._lookup_conn(uri)
        vm = conn.get_vm(uuid)

        logging.debug("Unpausing vm '%s'", vm.get_name())
        vmmAsyncJob.simple_async_noshow(vm.resume, [], src,
                                        _("Error unpausing domain"))

    def _do_run_domain(self, src, uri, uuid):
        conn = self._lookup_conn(uri)
        vm = conn.get_vm(uuid)

        logging.debug("Starting vm '%s'", vm.get_name())

        if vm.hasSavedImage():
            def errorcb(error, details):
                # This is run from the main thread
                res = src.err.show_err(
                    _("Error restoring domain") + ": " + error,
                    details=details,
                    text2=_(
                        "The domain could not be restored. Would you like\n"
                        "to remove the saved state and perform a regular\n"
                        "start up?"),
                    dialog_type=Gtk.MessageType.WARNING,
                    buttons=Gtk.ButtonsType.YES_NO,
                    async=False)

                if not res:
                    return

                try:
                    vm.removeSavedImage()
                    self._do_run_domain(src, uri, uuid)
                except Exception, e:
                    src.err.show_err(_("Error removing domain state: %s")
                                     % str(e))

            # VM will be restored, which can take some time, so show progress
            title = _("Restoring Virtual Machine")
            text = _("Restoring virtual machine memory from disk")
            vmmAsyncJob.simple_async(vm.startup,
                                     [], title, text, src, "", errorcb=errorcb)

        else:
            # Regular startup
            errorintro  = _("Error starting domain")
            vmmAsyncJob.simple_async_noshow(vm.startup, [], src, errorintro)

    def _do_shutdown_domain(self, src, uri, uuid):
        conn = self._lookup_conn(uri)
        vm = conn.get_vm(uuid)

        if not util.chkbox_helper(src, self.config.get_confirm_poweroff,
            self.config.set_confirm_poweroff,
            text1=_("Are you sure you want to poweroff '%s'?" %
                    vm.get_name())):
            return

        logging.debug("Shutting down vm '%s'", vm.get_name())
        vmmAsyncJob.simple_async_noshow(vm.shutdown, [], src,
                                        _("Error shutting down domain"))

    def _do_reboot_domain(self, src, uri, uuid):
        conn = self._lookup_conn(uri)
        vm = conn.get_vm(uuid)

        if not util.chkbox_helper(src, self.config.get_confirm_poweroff,
            self.config.set_confirm_poweroff,
            text1=_("Are you sure you want to reboot '%s'?" %
                    vm.get_name())):
            return

        logging.debug("Rebooting vm '%s'", vm.get_name())

        def reboot_cb():
            no_support = False
            reboot_err = None
            try:
                vm.reboot()
            except Exception, reboot_err:
                no_support = virtinst.util.is_error_nosupport(reboot_err)
                if not no_support:
                    raise RuntimeError(_("Error rebooting domain: %s" %
                                       str(reboot_err)))

            if not no_support:
                return

            # Reboot isn't supported. Let's try to emulate it
            logging.debug("Hypervisor doesn't support reboot, let's fake it")
            try:
                vm.manual_reboot()
            except:
                logging.exception("Could not fake a reboot")

                # Raise the original error message
                raise RuntimeError(_("Error rebooting domain: %s" %
                                   str(reboot_err)))

        vmmAsyncJob.simple_async_noshow(reboot_cb, [], src, "")

    def _do_reset_domain(self, src, uri, uuid):
        conn = self._lookup_conn(uri)
        vm = conn.get_vm(uuid)

        if not util.chkbox_helper(src, self.config.get_confirm_forcepoweroff,
            self.config.set_confirm_forcepoweroff,
            text1=_("Are you sure you want to force reset '%s'?" %
                    vm.get_name()),
            text2=_("This will immediately reset the VM without "
                    "shutting down the OS and may cause data loss.")):
            return

        logging.debug("Resetting vm '%s'", vm.get_name())
        vmmAsyncJob.simple_async_noshow(vm.reset, [], src,
                                        _("Error resetting domain"))

    def _do_delete_domain(self, src, uri, uuid):
        conn = self._lookup_conn(uri)
        vm = conn.get_vm(uuid)

        if not self.delete_dialog:
            self.delete_dialog = vmmDeleteDialog()
        self.delete_dialog.show(vm, src.topwin)
