# Copyright 2022-2023 Proemion GmbH
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import threading
import websocket
import datetime
import requests
import zipfile
import base64
import enum
import copy
import ssl
import os

from . import clmapi_pb2

class clm10k:
    """
    This class represents a connection instance to the CLM10k protobuf API.

    The CLM10k class provides easy to use methods to generate requests to the
    device via the websocket, using the protocol buffers based API.

    The responses from the unit are forwarded as is in proto messages, these
    messages already present the data in a well structured way, so it does
    not make much sense to repack it.

    Make sure to include the clmapi_pb2 module in your code.
    """

    class STATE_REQUEST(enum.Enum):
        NETWORK = enum.auto()
        DATAPORTAL = enum.auto()
        SYSTEM = enum.auto()
        FIRMWARE_UPDATE = enum.auto()
        SITEMANAGER = enum.auto()

    __slots__ = ("__ws", "__thread", "__message_id", "__device_info" ,
                 "__responses", "__cb_firmware_update", "__cb_config_update",
                 "__cb_connected", "__cb_closed", "__https_session",
                 "__https_url", "__cb_if_state")

    def __init__(self, ip, password, cb_connected = None,
                 cb_firmware_update = None, cb_config_update = None,
                 cb_disconnected = None, cb_if_state = None):
        """
        Parameters
        ----------
        ip[:port] : string
            IP address and optional port of the CLM10k unit. If port is not
            given, then port 443 will be used by default.

        password : string
            Admin password of the CLM10k unit.

        cb_connected : callback(device_info_data)
            This callback function which will be called once a connection to 
            the device is established. Device information will be passed to
            this function as the only parameter.

        cb_firmware_update : callback(firmware_update_info_data)
            This callback function will be called when a firmware update has
            been performed or attempted on the device. The update notification
            is sent out to all connected clients, regardless of who started the
            update.

            Locally the update can be started by HTTPS POST'ing the firmware
            image to the https://..device-ip../firmware URL.

        cb_config_update : callback(config_update_info_data)
            This callback function will be called when a configuration update 
            has been performed or attempted on the device. The update
            notification is sent out to all connected clients, regardless of
            who started the update.

            Locally the update can be started by HTTPS POST'ing the firmware
            image to the https://..device-ip../config URL.

        cb_disconnected : callback()
            This callback function will be called when a connection to the
            device has been closed. The callback is not bound to any
            specific logic, i.e. the callback will be triggered regardless
            if the connection has been closed due to user action like a call
            to terminate() or due to an error (for instance when an incorrect
            password has been supplied). It is up to the application to build
            up a logic in order to interpret the reason of the callback.

            Important: once disconnected a new instance needs to be
            instantiated in order to open a new connection.

        cb_if_state : callback()
            This callback function will be called when a network interface
            changes its state, it provides information whether an interface
            succeeded to establish a connection to a network.

        """

        self.__message_id = 0
        self.__responses = {}
        self.__device_info = clmapi_pb2.device_info_notification()
        if (cb_firmware_update != None) and (not callable(cb_firmware_update)):
            raise TypeError("firmware update callback must be a function")
        
        self.__cb_firmware_update = cb_firmware_update

        if (cb_config_update != None) and (not callable(cb_config_update)):
            raise TypeError("configuration update callback must be a function")

        self.__cb_config_update = cb_config_update

        if (cb_connected != None) and (not callable(cb_connected)):
            raise TypeError("connected callback must be a function")

        self.__cb_connected = cb_connected

        if (cb_disconnected != None) and (not callable(cb_disconnected)):
            raise TypeError("disconnected callback must be a function")

        self.__cb_closed = cb_disconnected

        if (cb_if_state != None) and (not callable(cb_if_state)):
            raise TypeError("network interface state callback must be a function")

        self.__cb_if_state = cb_if_state

        if not isinstance(ip, str):
            raise TypeError("IP address parameter must be a string")

        if ((ip == None) or (len(ip) == 0)):
            raise ValueError("missing IP address")

        if ":" not in ip:
            ip = ip + ":443"


        if not isinstance(password, str):
            raise ValueError("password parameter must be a string")

        if ((password == None) or (len(password) == 0)):
            raise ValueError("missing password")

        auth = base64.b64encode(f"admin:{password}".encode("ascii")).decode("ascii")
        auth_header = { "Authorization" : f"Basic {auth}" }

        self.__ws = websocket.WebSocketApp(f"wss://{ip}/wsproto",
                header = auth_header, on_message=self.__on_ws_message,
                on_error=self.__on_ws_error, on_close=self.__on_ws_close)

        # disable warnings about self signed certificates
        requests.urllib3.disable_warnings()

        self.__https_session = requests.Session()
        self.__https_session.auth = ("admin", password)
        self.__https_url = f"https://{ip}"
        self.__thread = threading.Thread(target=self.__thread_proc) 
        self.__thread.start()

    def terminate(self):
        """
        Terminate the websocket thread and close the websocket. The class can 
        not be used afterwards. If you want to connect again, a new instance
        needs to be created.
        """
        try:
            if self.__ws != None:
                self.__ws.close()

            if self.__thread != None:
                self.__thread.join()

            if self.__https_session != None:
                self.__https_session.close()

        except Exception:
            pass

    def __thread_proc(self):
        self.__ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE},
                              ping_interval = 5)

    def __on_ws_close(self, wsapp, status_code, message):
        if (callable(self.__cb_closed)):
            self.__cb_closed()

    def __on_ws_message(self, wsapp, message):
        response = clmapi_pb2.msg()
        try:
            response.ParseFromString(message)
        except Exception as e:
            print("failed to parse incoming proto message:", e)
            return

        callback = None
        cb_data = None

        try:
            if response.HasField("message_id") and \
                response.message_id in self.__responses:
                callback = self.__responses[response.message_id]

            if response.HasField("device_info_notification"):
                self.__device_info = response.device_info_notification
                if (callable(self.__cb_connected)):
                    self.__cb_connected(self.get_device_info())
            elif response.HasField("device_firmware_update_notification"):
                if (callable(self.__cb_firmware_update)):
                    self.__cb_firmware_update(
                            response.device_firmware_update_notification)
            elif response.HasField("device_config_update_notification"):
                if (callable(self.__cb_config_update)):
                    self.__cb_config_update(
                            response.device_config_update_notification)
            elif response.HasField("standard_response"):
                cb_data = response.standard_response
            elif response.HasField("get_config_response"):
                cb_data = response.get_config_response
            elif response.HasField("config_export_response"):
                cb_data = response.config_export_response
            elif response.HasField("apply_config_response"):
                cb_data = response.apply_config_response
            elif response.HasField("validate_config_response"):
                cb_data = response.validate_config_response
            elif response.HasField("get_config_group_info_response"):
                cb_data = response.get_config_group_info_response
            elif response.HasField("create_config_group_response"):
                cb_data = response.create_config_group_response
            elif response.HasField("get_datetime_response"):
                cb_data = response.get_datetime_response
            elif response.HasField("get_users_response"):
                cb_data = response.get_users_response
            elif response.HasField("get_state_response"):
                cb_data = response.get_state_response
            elif response.HasField("network_interface_state_notification"):
                if (callable(self.__cb_if_state)):
                    self.__cb_if_state(response.network_interface_state_notification)
            else:
                print("ignoring unknown submessage")

        except Exception as e:
            print("exception while processing message:", e)
            print(response)

        if response.HasField("message_id") and \
            response.message_id in self.__responses:
            del self.__responses[response.message_id]

        try:
            if (callable(callback)):
                callback(cb_data)
        except Exception as e:
            print(f"exception in user callback:", e)

    def __on_ws_error(self, wsapp, error):
        print(error)

    def __del__(self):
        try:
            self.terminate()
        except Exception:
            pass

    def __send_message(self, message, cb_response):
        message.message_id = self.__message_id
        self.__message_id = self.__message_id + 1
        self.__responses[message.message_id] = cb_response

        try:
            self.__ws.send(message.SerializeToString(),
                            websocket.ABNF.OPCODE_BINARY)
        except Exception as e:
            print(f"exception while sending message:", e)
            del self.__responses[message.message_id]

    def __check_init(self):
        if self.__ws == None:
            raise RuntimeError("websocket not initialized")

    # create a value checked tuple of two elements
    def __normalize_config_selector(self, item):
        result = ()

        if isinstance(item, int):
            result += (item, None)
        elif isinstance(item, tuple):
            if not isinstance(item[0], int):
                raise TypeError("first value of tuple must be an int")

            result += (item[0],)

            if (len(item) > 1):
                if not isinstance(item[1], str):
                    raise TypeError("second value of option tuple must \
                                     be a string (group name)")
                if len(item[1]) > 0:
                    result += (item[1],)
            else:
                result += (None,)

        else:
            raise TypeError("option must be either an int or a tuple \
                                    of (int,string)")

        return result
 
    def __create_value_from_option(self, element, opt, optval):
        optname = clmapi_pb2.cfg_option.Name(opt)
        try:
            if optname.startswith("CFG_OPTION_STR_"):
                element.value.str = str(optval)
            elif optname.startswith("CFG_OPTION_UINT16_"):
                val = int(str(optval))
                if ((val < 0) or (val > 65535)):
                    raise ValueError(f"value exceeds range of 0..65535")
                element.value.u16 = val
            elif optname.startswith("CFG_OPTION_UINT32_"):
                val = int(str(optval))
                if ((val < 0) or (val > 4294967295)):
                    raise ValueError(f"value exceeds range 0..4294967295")
                element.value.u32 = val
            elif optname.startswith("CFG_OPTION_INT32_"):
                val = int(str(optval))
                if ((val < -2147483648) or (val > 2147483647)):
                    raise ValueError(
                        f"value exceeds range of −2147483648..2147483647")
                element.value.s32 = val
            elif optname.startswith("CFG_OPTION_INT64_"):
                element.value.s64 = int(str(optval))
            elif optname.startswith("CFG_OPTION_BOOL_"):
                element.value.b = bool(int(str(optval)))
            elif optname.startswith("CFG_OPTION_BYTES_"):
                element.value.bytes = bytes(optval)
            else:
                raise ValueError(f"option {optname} is not known")

            return element

        except Exception as e:
            raise ValueError(f"failed to set value for option {optname}: {e}")

    def __create_apply_element(self, option):
        if (len(option) == 0):
            raise ValueError("missing value and option identifier")

        if (len(option) == 1):
            raise ValueError("missing option value")

        element = clmapi_pb2.cfg_apply_element()

        element = self.__create_value_from_option(element, option[0],
                option[1])
        element.option = option[0]
        if ((len(option) == 3) and (option[2] != None)):
            group_name = option[2]
            if (not isinstance(group_name, str)):
                raise TypeError(
                        f"group name for option {clmapi_pb2.cfg_option.Name(option[0])} is not a string")
            element.group_name = group_name

        return element

    def __add_config_options_and_groups(self, submsg, options, groups):
        if (options != None):
            if not isinstance(options, list):
                raise TypeError("options parameter must be a list")
           
            for option in options:
                selector = self.__normalize_config_selector(option)

                opt_sel = clmapi_pb2.cfg_option_selector()
                if (selector[0] != None):
                    opt_sel.option = selector[0]
                    if (selector[1] != None):
                        opt_sel.group_name = selector[1]
                submsg.option.append(opt_sel)

        if (groups != None):
            if not isinstance(groups, list):
                raise TypeError("groups parameter must be a list")

            for group in groups:
                selector = self.__normalize_config_selector(group)

                grp_sel = clmapi_pb2.cfg_group_selector()
                if (selector[0] != None):
                    grp_sel.group_type = selector[0]
                    if (selector[1] != None):
                        grp_sel.group_name = selector[1]
                submsg.group.append(grp_sel)

        return submsg

    def get_config(self, options = None, groups = None, get_defaults = False,
                   with_description = True, cb_response = None):
        """
        Retrieve one or more configuration settings.

        This function allows to retrieve configuration values of one or more
        settings. Either pass the option/group elements which should be read,
        or do not pass any at all - in this case all available settings will be
        returned.

        If the get_defaults parameter is set to true, then factory defaults of
        the given options will be retrieved instead of the currently set values.

        The logic for requesting groups works as follows:

        no groups + no options => return all options
        groups + no options =>    return options found in requested groups
        no groups + options =>    return requested options
        groups + options =>       return options found in requested
                                  groups + return individually specified options

        Note: requesting a group that is a parent to other subgroups will
              return all child options, including the ones found in sub groups.

        Parameters
        ----------
        options : [ (option_type, group_name), (option_type, ), ... ]
            Each list element must be a tuple where the first member of the
            tuple is the option type, while the second member is optional and
            is the associated group name which is required for options which 
            belong to dynamic groups.

        groups: [ (group_type, group_name), (group_type, ), ... ]
            Each list element must be a tuple where the first member of
            the tuple is the group type, while the second member is optional
            and is the group name, which is required for dynamic groups.

        get_defaults: bool
            Set this parameter to True in order to retrieve configuration
            defaults instead of actually set values.

        with_description: bool
            Set this parameter to True in order to enable human readable
            option descriptions in the response.

        cb_response: callable(response)
            Callback which will provide the response to this request.

        """
        self.__check_init()

        message = clmapi_pb2.msg()
        submsg = clmapi_pb2.get_config_request()
        submsg.get_defaults = get_defaults
        submsg.with_description = with_description

        submsg = self.__add_config_options_and_groups(submsg, options, groups)

        message.get_config_request.CopyFrom(submsg)
        self.__send_message(message, cb_response)

    def apply_config(self, options, cb_response = None):
        """
        Apply one or more application settings.

        Zero config elements are not allowed, i.e. if there is nothing to
        change, then don't call this function, otherwise the application will
        answer with an error response.
 
        Options that belong to the same group will be automatically handled as
        "transactions", i.e. all options from the group must succeed, if one
        fails, then the whole group won't get applied.

        Parameters
        ----------
        options : [ (option_type, value, group_name), (option_type, value,),.. ]
            Each list element must be a tuple where the first member of the
            tuple is the option type, while the second member is optional and
            is the associated group name which is required for options which
            belong to dynamic groups.

        cb_response: callable(response)
            Callback which will provide the response to this request.

        """

        self.__check_init()

        if (not isinstance(options, list)):
            raise TypeError("options parameter is missing or is not a list")

        if (len(options) == 0):
            raise ValueError("no options to apply - empty list")

        message = clmapi_pb2.msg()
        submsg = clmapi_pb2.apply_config_request()

        for option in options:
            if (not isinstance(option, tuple)):
                raise TypeError(
                    "options parameter list element must be a tuple")

            element = self.__create_apply_element(option)
            if (element != None):
                submsg.element.append(element)

        message.apply_config_request.CopyFrom(submsg)
        self.__send_message(message, cb_response)

    def validate_config(self, options, cb_response = None):
        """
        Validate one or more application settings.

        Zero config elements are not allowed, i.e. if there is nothing to
        validate, then don't call this function, otherwise the application will
        answer with an error response.

        Parameters
        ----------
        options : [ (option_type, value, group_name), (option_type, value,),.. ]
            Each list element must be a tuple where the first member of the
            tuple is the option type, while the second member is optional and
            is the associated group name which is required for options which
            belong to dynamic groups.

        cb_response: callable(response)
            Callback which will provide the response to this request.

        """

        self.__check_init()

        if (not isinstance(options, list)):
            raise TypeError("options parameter is missing or is not a list")

        if (len(options) == 0):
            raise ValueError("no options to apply - empty list")

        message = clmapi_pb2.msg()
        submsg = clmapi_pb2.validate_config_request()

        for option in options:
            if (not isinstance(option, tuple)):
                raise TypeError(
                    "options parameter list element must be a tuple")

            element = self.__create_apply_element(option)
            if (element != None):
                submsg.element.append(element)

        message.validate_config_request.CopyFrom(submsg)
        self.__send_message(message, cb_response)

    def get_config_group_info(self, groups = [], with_description = True,
                              recursive = True, cb_response = None):
        """
        Request information about configuration groups and their parent-child
        relationship.

        Parameters
        ----------
        groups : [ group_type, group_type, ...]
            List of group types for which to request the information.

        with_description : bool
            Controls presence of human readable group descriptions in the
            response.

        recursive : bool
            Parameter which controls if the group information should be
            returned recursively or if only the top level groups are
            returned.

        cb_response: callable(response)
            Callback which will provide the response to this request.

        """
        self.__check_init()

        if (not isinstance(groups, list)):
            raise TypeError("groups parameter must be a list")

        message = clmapi_pb2.msg()
        submsg = clmapi_pb2.get_config_group_info_request()
        submsg.with_description = with_description
        submsg.recursive = recursive

        for group in groups:
            submsg.group_type.append(group)

        message.get_config_group_info_request.CopyFrom(submsg)
        self.__send_message(message, cb_response)
       
    def create_config_group(self, group, name, cb_response = None):
        """
        Create a new configuration group.
 
        This function creates a configuration group for the requested group 
        type, this is how network profiles can be created. A unique name must
        be provided, names span across all dynamic groups regardless of the
        group type.

        Only dynamic group types are allowed.

        Parameters
        ----------
        group : int
            Type of the group to create.

        name : str
            Name of the new group.

         cb_response: callable(response)
            Callback which will provide the response to this request.
           
        """
        self.__check_init()

        if (not isinstance(group, int)):
            raise TypeError("invalid group type")

        if (not isinstance(name, str)):
            raise TypeError("group name parameter must be a string")

        if (len(name) == 0):
            raise ValueError("group name can not be empty")

        message = clmapi_pb2.msg()
        submsg = clmapi_pb2.create_config_group_request()
        submsg.group_type = group
        submsg.group_name = name

        message.create_config_group_request.CopyFrom(submsg)
        self.__send_message(message, cb_response)
       
    def remove_config_group(self, name, cb_response = None):
        """
		Remove a configuration group.

		This function removes a configuration group with the given name, this 
		is how network connection profiles can be removed. Note, that the last
		profile for each interface can not be deleted, attempting to do so
		will result in an error.

		Only dynamic groups can be removed, since group names are
		unique, it is not necessary to supply the group type.

		Parameters
        ----------
        name: str
            Name of the group which needs to be deleted.

        cb_response: callable(response)
            Callback which will provide the response to this request.
        """
        self.__check_init()

        if (not isinstance(name, str)):
            raise TypeError("group name parameter must be a string")

        if (len(name) == 0):
            raise ValueError("group name can not be empty")

        message = clmapi_pb2.msg()
        submsg = clmapi_pb2.remove_config_group_request()
        submsg.group_name = name

        message.remove_config_group_request.CopyFrom(submsg)
        self.__send_message(message, cb_response)

    def config_export(self, options = None, groups = None,
                      factory_reset = None, cb_response = None):
        """
        Export device configuration to a file that can later be reimported.

        If all fields are left out, then all writable config options
        will be exported.
 
        If the factory_reset parameter is true, then the exported file will
        contain only the reset to factory defaults instruction. Options and
        groups parameters must not be set.

        Parameters
        ----------
        options: [ (option_type, group_name), (option_type, ), ...]
            Options to export, list of tuples containing the option type
            as the first tuple member and an optional group name as the
            second tuple member. The group name is required for options
            that belong to a dynamic group.
            
        groups: [ (group_type, group_name), (group_type, ), ... ]
            Groups to export, list of tuples containing the group type
            as the first member and an optional group name as the second
            tuple member. The group name is required for dynamic groups.

        factory_reset: bool
            If this parameter is set, the configuration export will only
            contain the reset instruction, options and groups parameters must
            not be set.

        cb_response: callable(response)
            Callback which will provide the response to this request.
        """

        self.__check_init()

        message = clmapi_pb2.msg()
        submsg = clmapi_pb2.config_export_request()
        if (factory_reset != None) and (isinstance(factory_reset, bool)):
            submsg.factory_reset = factory_reset

        if (factory_reset == None) or (factory_reset == False):
            submsg = self.__add_config_options_and_groups(submsg, options,
                                                              groups)

        message.config_export_request.CopyFrom(submsg)
        self.__send_message(message, cb_response)

    def __reset_device(self, reset_type, cb_response = None):
        self.__check_init()

        message = clmapi_pb2.msg()
        submsg = clmapi_pb2.device_reset_request()
        submsg.reset_type = reset_type
        message.device_reset_request.CopyFrom(submsg)
        self.__send_message(message, cb_response)

    def reset_to_factory_settings(self, cb_response = None):
        """
        Reset device to factory settings.

        The factory reset deletes all data on the device and restores
        the original factory settings.
        Note, that the device will be rebooted, meaning that the
        websocket connection will be interrupted.

        Parameters
        ----------
        cb_response: callable(response)
            Callback which will provide the response to this request.
        """

        self.__reset_device(clmapi_pb2.DEVICE_RESET_FACTORY, cb_response)

    def reset_configuration(self, cb_response = None):
        """
        Reset device configuration to factory defaults.

        Note, that the configuration reset only restores the application
        settings to factory defaults and does not affect credentials
        nor removes created users.
        The application will be restarted, meaning that the websocket connection
        will be interrupted.

        Parameters
        ----------
        cb_response: callable(response)
            Callback which will provide the response to this request.
        """

        self.__reset_device(clmapi_pb2.DEVICE_RESET_CONFIGURATION, cb_response)

    def firmware_update_abort(self, cb_response = None):
        """
        Abort an ongoing firmware update.

        This command can only cancel a pending or ongoing download, the flashing
        process can not be interrupted.

        Parameters
        ----------
        cb_response: callable(response)
            Callback which will provide the response to this request.
        """

        self.__check_init()

        message = clmapi_pb2.msg()
        submsg = clmapi_pb2.firmware_update_abort_request()
        message.firmware_update_abort_request.CopyFrom(submsg)
        self.__send_message(message, cb_response)
       
    def get_device_info(self):
        """
        Retrieve device information.

        This will return the same data as the cb_connected callback.
        """
        return copy.deepcopy(self.__device_info)

    def set_password(self, username, admin_pass, new_pass, cb_response = None):
        """
        Change the password for the web server of the device, this affects
        access to the web UI and to this protocol buffers API.

        The old password is always required, the new password will only be set 
        if the old one is correct.

        Currently only two users are supported: "admin" has access to
        everything while "upload" can only POST files for further transfer to
        the DataPortal, but has no access to the protocol buffers API.

        Parameters
        ----------
        username : str
            Name of the user for which the password needs to be set. Currently
            either "admin" or "upload.

        admin_pass : str
            Current admin password which is required for the validation of this
            call.

        new_pass : str
            New password for the selected user.

        cb_response: callable(response)
            Callback which will provide the response to this request.
        """

        self.__check_init()

        def trap_response(data):
            if (username == "admin") and (data.result_code == clmapi_pb2.RC_OK):
                self.__https_session.auth = ("admin", new_pass)

            if cb_response != None:
                cb_response(data)

        message = clmapi_pb2.msg()
        submsg = clmapi_pb2.set_password_request()
        submsg.username = username
        submsg.admin_password = admin_pass
        submsg.new_password = new_pass
        message.set_password_request.CopyFrom(submsg)
        self.__send_message(message, trap_response)

    def remove_upload_user(self, cb_response = None):
        """
        Remove the "upload" user.

        Parameters
        ----------
        cb_response: callable(response)
            Callback which will provide the response to this request.
        """
        self.__check_init()

        message = clmapi_pb2.msg()
        submsg = clmapi_pb2.remove_user_request()
        submsg.username = "upload" # only this user can be removed
        message.remove_user_request.CopyFrom(submsg)
        self.__send_message(message, cb_response)

    def get_users(self, cb_response = None):
        """
        Get a list of currently available user accounts.

        Parameters
        ----------
        cb_response: callable(response)
            Callback which will provide the response to this request.
        """

        self.__check_init()

        message = clmapi_pb2.msg()
        submsg = clmapi_pb2.get_users_request()
        message.get_users_request.CopyFrom(submsg)
        self.__send_message(message, cb_response)

    def set_datetime(self, timestamp = None, cb_response = None):
        """
        Set date and time.

        Parameters
        ----------
        timestamp:  datetime.datetime()
            Datetime object containing a timestamp which should be applied to
            the device.
        """

        self.__check_init()

        if (not isinstance(timestamp, datetime.datetime)):
            raise TypeError("timestamp must be a datetime.datetime() object")

        message = clmapi_pb2.msg()
        submsg = clmapi_pb2.set_datetime_request()
        submsg.utc_seconds_from_epoch = int(timestamp.timestamp())
        message.set_datetime_request.CopyFrom(submsg)
        self.__send_message(message, cb_response)

    def get_datetime(self, cb_response = None):
        """
        Get date and time on the device.

        """

        self.__check_init()

        message = clmapi_pb2.msg()
        submsg = clmapi_pb2.get_datetime_request()
        message.get_datetime_request.CopyFrom(submsg)
        self.__send_message(message, cb_response)

    def get_state(self, request_states = [], cb_response = None):
        """
        Get various volatile device states. If none are passed, then all
        states will be returned. Otherwise supply the states of interest
        in the array, the states must by members of the STATE_REQUEST enum.

        Parameters
        ----------
        request_states: [ clm10k.STATE_REQUEST.NETWORK, clm10k.STATE_REQUEST.SYSTEM, ...]
            States to retrieve, omit parameter or pass an empty array to
            retrieve all states.

        """

        self.__check_init()

        message = clmapi_pb2.msg()
        submsg = clmapi_pb2.get_state_request()

        if not isinstance(request_states, list):
            raise TypeError("request_states parameter is not a list")

        if len(request_states) > 0:
            if clm10k.STATE_REQUEST.NETWORK in request_states:
                submsg.state_network = True

            if clm10k.STATE_REQUEST.DATAPORTAL in request_states:
                submsg.state_dataportal = True

            if clm10k.STATE_REQUEST.SYSTEM in request_states:
                submsg.state_system = True

            if clm10k.STATE_REQUEST.SITEMANAGER in request_states:
                submsg.state_sitemanager = True

            if clm10k.STATE_REQUEST.FIRMWARE_UPDATE in request_states:
                submsg.state_firmware_update = True

        message.get_state_request.CopyFrom(submsg)
        self.__send_message(message, cb_response)

    def __http_post(self, filename, identifier, uri):
        self.__check_init()

        if (not isinstance(filename, str)):
            raise TypeError("filename parameter must be a string")

        if (len(filename) == 0):
            raise ValueError("filename parameter must not be empty")

        if not os.path.isfile(filename):
            raise IOError(f"{identifier} '{filename}' not found")

        try:
            f = { "file": open(filename, "rb") }
            result = self.__https_session.post(f"{self.__https_url}/{uri}",
                                               files = f, verify = False)
            code = result.status_code
            reason = result.reason

            if code != requests.codes.OK:
                raise RuntimeError(f"{reason} (HTTP {code})")
        except requests.exceptions.SSLError:
            raise RuntimeError(f"failed to transfer {filename} to the device")
        except Exception as e:
            raise RuntimeError(
                    f"failed to transfer {filename} to the device: {e}")


    def ssh_copy_id(self, filename):
        """
        Install the given ssh key on the unit.

        Note: this function is synchronous.
        """
        self.__http_post(filename, "public key", "ssh")

    def firmware_update(self, filename):
        """
        Perform a firmware update using the provided image.

        Note: this function is synchronous.
        """
        
        self.__http_post(filename, "firmware image", "firmware")

    def cfg_import(self, filename):
        """
        Import configuration from file.

        Note: this function is synchronous.
        """

        self.__http_post(filename, "configuration file", "config")

    def upload_file(self, filename):
        """
        Upload a file to the device for further transfer to the DataPortal.

        Note: this function is synchronous.
        """

        self.__http_post(filename, "file", "upload")

    @staticmethod
    def decode_config(filename):
        """
        Decode a previously exported configuration file and return the decoded
        data structure.

        Note: this function is synchronous.

        Parameters
        ----------
        filename: configuration file to decode

        """

        if (not isinstance(filename, str)):
            raise TypeError("filename parameter must be a string")

        if (len(filename) == 0):
            raise ValueError("filename parameter must not be empty")

        if not os.path.isfile(filename):
            raise IOError(f"file '{filename}' not found")

        try:
            with zipfile.ZipFile(filename) as zip_file:
                cfg_file_data = zip_file.read("clm10k-config")
                cfg = clmapi_pb2.config_file()
                cfg.ParseFromString(cfg_file_data)
                return cfg

        except KeyError: # we'll get here if clm10k-config was not in the zip
            raise ValueError(
                f"file {filename} is not a valid clm10k configuration file")

        except Exception as e:
            raise RuntimeError(f"failed to decode configuration: {e}")


    def set_power_state(self, power_command_type, cb_response = None):
        """
        Control the power state of the device.

        Depending on the power command type, the device shuts down, then remains off,
        wakes up on configured events or restars immediately.

        Parameters
        ----------
        power_command_type: enum clmapi_pb2.power_command_type
            Power command type. POWER_COMMAND_TYPE_SHUTDOWN turns the device off immediately,
            and the system remains off regardless of the wake-up configuration.
            This command is intended to be used in the testing environment,
            as the device will start again only on the power cycle of the Clamp30.
            POWER_COMMAND_TYPE_REBOOT type shuts down the system and then restarts
            immediately. POWER_COMMAND_TYPE_STANDBY puts the device into sleep mode
            when the Clamp15 goes low and wakes up on configured wake-up events.
        cb_response: callable(response)
            Callback which will provide the response to this request.
        """

        self.__check_init()

        message = clmapi_pb2.msg()
        submsg = clmapi_pb2.power_state_request()

        if power_command_type not in [clmapi_pb2.POWER_COMMAND_TYPE_SHUTDOWN,
                               clmapi_pb2.POWER_COMMAND_TYPE_REBOOT,
                               clmapi_pb2.POWER_COMMAND_TYPE_STANDBY]:
                raise ValueError("Invalid power state")

        submsg.power_command_type = power_command_type

        message.set_power_state_request.CopyFrom(submsg)
        self.__send_message(message, cb_response)