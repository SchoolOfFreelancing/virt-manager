#
# Copyright 2011, 2013 Red Hat, Inc.
# Copyright 2013 IBM Corporation
#
# This work is licensed under the GNU GPLv2 or later.
# See the COPYING file in the top-level directory.

from .device import Device
from ..xmlbuilder import XMLProperty


class DeviceTpm(Device):
    XML_NAME = "tpm"

    VERSION_1_2 = "1.2"
    VERSION_2_0 = "2.0"
    VERSIONS = [VERSION_1_2, VERSION_2_0]

    TYPE_PASSTHROUGH = "passthrough"
    TYPE_EMULATOR = "emulator"
    TYPES = [TYPE_PASSTHROUGH, TYPE_EMULATOR]

    MODEL_TIS = "tpm-tis"
    MODEL_CRB = "tpm-crb"
    MODELS = [MODEL_TIS, MODEL_CRB]

    @staticmethod
    def get_pretty_type(tpm_type):
        if tpm_type == DeviceTpm.TYPE_PASSTHROUGH:
            return _("Passthrough device")
        if tpm_type == DeviceTpm.TYPE_EMULATOR:
            return _("Emulated device")
        return tpm_type

    @staticmethod
    def get_pretty_model(tpm_model):
        if tpm_model == DeviceTpm.MODEL_TIS:
            return _("TIS")
        if tpm_model == DeviceTpm.MODEL_CRB:
            return _("CRB")
        return tpm_model

    type = XMLProperty("./backend/@type")
    version = XMLProperty("./backend/@version")
    model = XMLProperty("./@model")
    device_path = XMLProperty("./backend/device/@path")


    ##################
    # Default config #
    ##################

    def set_defaults(self, guest):
        if not self.type:
            self.type = self.TYPE_PASSTHROUGH
        if not self.model:
            self.model = self.MODEL_TIS
