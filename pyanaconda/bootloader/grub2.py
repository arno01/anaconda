#
# Copyright (C) 2019 Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#
import os
from _ped import PARTITION_BIOS_GRUB

from blivet.devicelibs import raid

from pyanaconda.bootloader.base import BootLoaderError
from pyanaconda.bootloader.grub import GRUB
from pyanaconda.core import util
from pyanaconda.core.configuration.anaconda import conf
from pyanaconda.core.i18n import _
from pyanaconda.product import productName

from pyanaconda.anaconda_loggers import get_module_logger
log = get_module_logger(__name__)

__all__ = ["GRUB2", "IPSeriesGRUB2"]


class GRUB2(GRUB):
    """GRUBv2.

    - configuration
        - password (insecure), password_pbkdf2
          http://www.gnu.org/software/grub/manual/grub.html#Invoking-grub_002dmkpasswd_002dpbkdf2
        - users per-entry specifies which users can access, otherwise entry is unrestricted
        - /etc/grub/custom.cfg

    - how does grub resolve names of md arrays?

    - disable automatic use of grub-mkconfig?
        - on upgrades?

    - BIOS boot partition (GPT)
        - parted /dev/sda set <partition_number> bios_grub on
        - can't contain a file system
        - 31KiB min, 1MiB recommended
    """
    name = "GRUB2"
    # grub2 is a virtual provides that's provided by grub2-pc, grub2-ppc64le,
    # and all of the primary grub components that aren't grub2-efi-${EFIARCH}
    packages = ["grub2", "grub2-tools"]
    _config_file = "grub.cfg"
    _config_dir = "grub2"
    _passwd_file = "user.cfg"
    defaults_file = "/etc/default/grub"
    terminal_type = "console"
    stage2_max_end = None

    # requirements for boot devices
    stage2_device_types = ["partition", "mdarray"]
    stage2_raid_levels = [raid.RAID0, raid.RAID1, raid.RAID4,
                          raid.RAID5, raid.RAID6, raid.RAID10]
    stage2_raid_metadata = ["0", "0.90", "1.0", "1.2"]

    # XXX we probably need special handling for raid stage1 w/ gpt disklabel
    #     since it's unlikely there'll be a bios boot partition on each disk

    @property
    def stage2_format_types(self):
        if productName.startswith("Red Hat "): # pylint: disable=no-member
            return ["xfs", "ext4", "ext3", "ext2"]
        else:
            return ["ext4", "ext3", "ext2", "btrfs", "xfs"]

    #
    # grub-related conveniences
    #

    def grub_device_name(self, device):
        """Return a grub-friendly representation of device.

        Disks and partitions use the (hdX,Y) notation, while lvm and
        md devices just use their names.
        """
        disk = None
        name = "(%s)" % device.name

        if device.is_disk:
            disk = device
        elif hasattr(device, "disk"):
            disk = device.disk

        if disk is not None:
            name = "(hd%d" % self.disks.index(disk)
            if hasattr(device, "disk"):
                lt = device.disk.format.label_type
                name += ",%s%d" % (lt, device.parted_partition.number)
            name += ")"
        return name

    def write_config_console(self, config):
        if not self.console:
            return

        console_arg = "console=%s" % self.console
        if self.console_options:
            console_arg += ",%s" % self.console_options
        self.boot_args.add(console_arg)

    def write_device_map(self):
        """Write out a device map containing all supported devices."""
        map_path = os.path.normpath(util.getSysroot() + self.device_map_file)
        if os.access(map_path, os.R_OK):
            os.rename(map_path, map_path + ".anacbak")

        devices = self.disks
        if self.stage1_device not in devices:
            devices.append(self.stage1_device)

        for disk in self.stage2_device.disks:
            if disk not in devices:
                devices.append(disk)

        devices = [d for d in devices if d.is_disk]

        if len(devices) == 0:
            return

        dev_map = open(map_path, "w")
        dev_map.write("# this device map was generated by anaconda\n")
        for drive in devices:
            dev_map.write("%s      %s\n" % (self.grub_device_name(drive),
                                            drive.path))
        dev_map.close()

    def write_defaults(self):
        defaults_file = "%s%s" % (util.getSysroot(), self.defaults_file)
        defaults = open(defaults_file, "w+")
        defaults.write("GRUB_TIMEOUT=%d\n" % self.timeout)
        defaults.write("GRUB_DISTRIBUTOR=\"$(sed 's, release .*$,,g' /etc/system-release)\"\n")
        defaults.write("GRUB_DEFAULT=saved\n")
        defaults.write("GRUB_DISABLE_SUBMENU=true\n")
        if self.console and self.has_serial_console:
            defaults.write("GRUB_TERMINAL=\"serial console\"\n")
            defaults.write("GRUB_SERIAL_COMMAND=\"%s\"\n" % self.serial_command)
        else:
            defaults.write("GRUB_TERMINAL_OUTPUT=\"%s\"\n" % self.terminal_type)

        # this is going to cause problems for systems containing multiple
        # linux installations or even multiple boot entries with different
        # boot arguments
        log.info("bootloader.py: used boot args: %s ", self.boot_args)
        defaults.write("GRUB_CMDLINE_LINUX=\"%s\"\n" % self.boot_args)
        defaults.write("GRUB_DISABLE_RECOVERY=\"true\"\n")
        #defaults.write("GRUB_THEME=\"/boot/grub2/themes/system/theme.txt\"\n")

        if self.use_bls and os.path.exists(util.getSysroot() + "/usr/sbin/new-kernel-pkg"):
            log.warning("BLS support disabled due new-kernel-pkg being present")
            self.use_bls = False

        if self.use_bls:
            defaults.write("GRUB_ENABLE_BLSCFG=true\n")
        defaults.close()

    def _encrypt_password(self):
        """Make sure self.encrypted_password is set up properly."""
        if self.encrypted_password:
            return

        if not self.password:
            raise RuntimeError("cannot encrypt empty password")

        (pread, pwrite) = os.pipe()
        passwords = "%s\n%s\n" % (self.password, self.password)
        os.write(pwrite, passwords.encode("utf-8"))
        os.close(pwrite)
        buf = util.execWithCapture("grub2-mkpasswd-pbkdf2", [],
                                   stdin=pread,
                                   root=util.getSysroot())
        os.close(pread)
        self.encrypted_password = buf.split()[-1].strip()
        if not self.encrypted_password.startswith("grub.pbkdf2."):
            raise BootLoaderError("failed to encrypt boot loader password")

    def write_password_config(self):
        if not self.password and not self.encrypted_password:
            return

        users_file = "%s%s/%s" % (util.getSysroot(), self.config_dir, self._passwd_file)
        header = util.open_with_perm(users_file, "w", 0o700)
        # XXX FIXME: document somewhere that the username is "root"
        self._encrypt_password()
        password_line = "GRUB2_PASSWORD=" + self.encrypted_password
        header.write("%s\n" % password_line)
        header.close()

    def write_config(self):
        self.write_config_console(None)
        # See if we have a password and if so update the boot args before we
        # write out the defaults file.
        if self.password or self.encrypted_password:
            self.boot_args.add("rd.shell=0")
        self.write_defaults()

        # if we fail to setup password auth we should complete the
        # installation so the system is at least bootable
        try:
            self.write_password_config()
        except (BootLoaderError, OSError, RuntimeError) as e:
            log.error("boot loader password setup failed: %s", e)

        # make sure the default entry is the OS we are installing
        if self.default is not None:
            # find the index of the default image
            try:
                default_index = self.images.index(self.default)
            except ValueError:
                # pylint: disable=no-member
                log.warning("Failed to find default image (%s), defaulting to 0",
                            self.default.label)
                default_index = 0

            rc = util.execInSysroot("grub2-set-default", [str(default_index)])
            if rc:
                log.error("failed to set default menu entry to %s", productName)

        # set menu_auto_hide grubenv variable if we should enable menu_auto_hide
        # set boot_success so that the menu is hidden on the boot after install
        if self.menu_auto_hide:
            rc = util.execInSysroot("grub2-editenv",
                                    ["-", "set", "menu_auto_hide=1",
                                     "boot_success=1"])
            if rc:
                log.error("failed to set menu_auto_hide=1")

        # now tell grub2 to generate the main configuration file
        rc = util.execInSysroot("grub2-mkconfig",
                                ["-o", self.config_file])
        if rc:
            raise BootLoaderError("failed to write boot loader configuration")

    #
    # installation
    #

    def install(self, args=None):
        if args is None:
            args = []

        # XXX will installing to multiple drives work as expected with GRUBv2?
        for (stage1dev, stage2dev) in self.install_targets:
            grub_args = args + ["--no-floppy", stage1dev.path]
            if stage1dev == stage2dev:
                # This is hopefully a temporary hack. GRUB2 currently refuses
                # to install to a partition's boot block without --force.
                grub_args.insert(0, '--force')
            else:
                if self.keep_mbr:
                    grub_args.insert(0, '--grub-setup=/bin/true')
                    log.info("bootloader.py: mbr update by grub2 disabled")
                else:
                    log.info("bootloader.py: mbr will be updated for grub2")

            rc = util.execWithRedirect("grub2-install", grub_args,
                                       root=util.getSysroot(),
                                       env_prune=['MALLOC_PERTURB_'])
            if rc:
                raise BootLoaderError("boot loader install failed")

    def write(self):
        """Write the bootloader configuration and install the bootloader."""
        if self.skip_bootloader:
            return

        if self.update_only:
            self.update()
            return

        try:
            self.write_device_map()
            self.stage2_device.format.sync(root=util.getTargetPhysicalRoot())
            os.sync()
            self.install()
            os.sync()
            self.stage2_device.format.sync(root=util.getTargetPhysicalRoot())
        finally:
            self.write_config()
            os.sync()
            self.stage2_device.format.sync(root=util.getTargetPhysicalRoot())

    def check(self):
        """When installing to the mbr of a disk grub2 needs enough space
        before the first partition in order to embed its core.img

        Until we have a way to ask grub2 what the size is we check to make
        sure it starts >= 512K, otherwise return an error.
        """
        ret = True
        base_gap_bytes = 32256       # 31.5KiB
        advanced_gap_bytes = 524288  # 512KiB
        self.errors = []
        self.warnings = []

        if self.stage1_device == self.stage2_device:
            return ret

        # These are small enough to fit
        if self.stage2_device.type == "partition":
            min_start = base_gap_bytes
        else:
            min_start = advanced_gap_bytes

        if not self.stage1_disk:
            return False

        # If the first partition starts too low and there is no biosboot partition show an error.
        error_msg = None
        biosboot = False
        parts = self.stage1_disk.format.parted_disk.partitions
        for p in parts:
            if p.getFlag(PARTITION_BIOS_GRUB):
                biosboot = True
                break

            start = p.geometry.start * p.disk.device.sectorSize
            if start < min_start:
                error_msg = _("%(deviceName)s may not have enough space for grub2 to embed "
                              "core.img when using the %(fsType)s file system on %(deviceType)s") \
                              % {"deviceName": self.stage1_device.name,
                                 "fsType": self.stage2_device.format.type,
                                 "deviceType": self.stage2_device.type}

        if error_msg and not biosboot:
            log.error(error_msg)
            self.errors.append(error_msg)
            ret = False

        return ret


class IPSeriesGRUB2(GRUB2):
    """IPSeries GRUBv2"""

    # GRUB2 sets /boot bootable and not the PReP partition. This causes the Open Firmware BIOS
    # not to present the disk as a bootable target. If stage2_bootable is False, then the PReP
    # partition will be marked bootable. Confusing.

    stage2_bootable = False
    terminal_type = "ofconsole"

    #
    # installation
    #

    def install(self, args=None):
        if self.keep_boot_order:
            log.info("leavebootorder passed as an option. Will not update the NVRAM boot list.")
        else:
            self.updateNVRAMBootList()

        super().install(args=["--no-nvram"])

    # This will update the PowerPC's (ppc) bios boot devive order list
    def updateNVRAMBootList(self):
        if not conf.target.is_hardware:
            return

        log.debug("updateNVRAMBootList: self.stage1_device.path = %s", self.stage1_device.path)

        buf = util.execWithCapture("nvram",
                                   ["--print-config=boot-device"])

        if len(buf) == 0:
            log.error("Failed to determine nvram boot device")
            return

        boot_list = buf.strip().replace("\"", "").split()
        log.debug("updateNVRAMBootList: boot_list = %s", boot_list)

        buf = util.execWithCapture("ofpathname",
                                   [self.stage1_device.path])

        if len(buf) > 0:
            boot_disk = buf.strip()
        else:
            log.error("Failed to translate boot path into device name")
            return

        # Place the disk containing the PReP partition first.
        # Remove all other occurances of it.
        boot_list = [boot_disk] + [x for x in boot_list if x != boot_disk]

        update_value = "boot-device=%s" % " ".join(boot_list)

        rc = util.execWithRedirect("nvram", ["--update-config", update_value])
        if rc:
            log.error("Failed to update new boot device order")

    #
    # In addition to the normal grub configuration variable, add one more to set the size
    # of the console's window to a standard 80x24
    #
    def write_defaults(self):
        super().write_defaults()

        defaults_file = "%s%s" % (util.getSysroot(), self.defaults_file)
        defaults = open(defaults_file, "a+")
        # The terminfo's X and Y size, and output location could change in the future
        defaults.write("GRUB_TERMINFO=\"terminfo -g 80x24 console\"\n")
        # Disable OS Prober on pSeries systems
        # TODO: This will disable across all POWER platforms. Need to get
        #       into blivet and rework how it segments the POWER systems
        #       to allow for differentiation between PowerNV and
        #       PowerVM / POWER on qemu/kvm
        defaults.write("GRUB_DISABLE_OS_PROBER=true\n")
        defaults.close()