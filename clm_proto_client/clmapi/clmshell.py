#!/usr/bin/env python3
#
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

import os
import sys
import time
import datetime
import threading
import pyparsing as pp

from clmapi import clmlib
from clmapi import clmapi_pb2

from dateutil import parser
from tabulate import tabulate

from prompt_toolkit import prompt
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.completion import NestedCompleter, WordCompleter
from prompt_toolkit.completion import PathCompleter
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.completion import Completer, Completion

class OptionCompleter(Completer):
    __completions = []

    def __init__(self, completions):
        self.__completions = completions

    def get_completions(self, document, complete_event):
        try:
            line = document.text_before_cursor

            group_name = pp.Combine(pp.Literal(':') + \
                        pp.SkipTo(pp.Literal(':') | pp.StringEnd(), \
                        include=True))
            option = pp.Word(pp.alphanums + "_")
            option_with_group = \
                pp.Combine(option + pp.Optional(group_name) + \
                            pp.Optional(pp.White(" ")))
            options = (pp.ZeroOrMore(option_with_group))

            unparsed_line = line
            parsed = options.parseString(line)
            # we are only interested in the very last argument since it will be
            # the one that needs to be completed
            if (len(parsed) > 1):
                line = parsed.pop()

            completions = []
            completions = [ opt for opt in self.__completions \
                                if opt.startswith(line)]

            for completion in completions:
                yield Completion(text = completion,
                    start_position = -len(line),
                    display = completion,
                    display_meta = completion)

        except Exception as e:
            print(f"Exception {e}")

class ApplyOptionCompleter(Completer):
    __completions = []
    __allowed_values = {}

    def __init__(self, completions, allowed_values):
        self.__completions = completions
        self.__allowed_values = allowed_values

    def get_completions(self, document, complete_event):
        try:
            line = document.text_before_cursor

            num_value = (pp.Word(pp.nums) + \
                     pp.Optional("." + pp.OneOrMore(pp.Word(pp.nums))))
            value = (pp.QuotedString(quote_char = "\"", esc_char = "\\",
                     unquote_results = False) | num_value)

            group_name = pp.Combine(pp.Literal(":") + \
                        pp.SkipTo(pp.Literal(":") | pp.StringEnd(), \
                        include=True))

            opt_id = (pp.Word(pp.alphanums + "_"))
            opt_id_with_value = pp.Combine(opt_id + pp.Literal("=") + value + \
                                           pp.Optional(pp.White(" ")))
            opt_id_with_group_and_value = pp.Combine(opt_id + \
                                      pp.Optional(group_name) + \
                                      pp.Literal("=") + pp.Group(value) + \
                                      pp.Optional(pp.White(" ")))

            options = pp.Combine(pp.ZeroOrMore(opt_id_with_group_and_value | \
                                     opt_id_with_value))

            unparsed_line = line
            parsed = options.parseString(line)
            # we are only interested in the very last argument since it will be
            # the one that needs to be completed
            cut = 0
            for p in parsed:
                cut = cut + len(p)

            if (cut > 0):
                line = line[cut:]

            completions = []
            completions = [ opt for opt in self.__completions \
                                if opt.startswith(line) ]

            # we do not want to display all allowed value options right
            # away in the option name completion, but we want to suggest
            # the allowed values only after the option name has been
            # completed so that the completion list will contain the allowed
            # values and nothing else
            if len(completions) == 1:
                candidate = completions[0]

                if candidate in self.__allowed_values:
                    completions = self.__allowed_values[candidate]

            # self.__completion is now shorter than the line which already
            # contains a portion of the allowed-value, so we have to turn
            # the search around and first find the matching key to get to
            # the list of allowed values and then look up the value completion
            if (len(line) > 0) and (len(completions) == 0):
                reverse = None
                for key in self.__allowed_values.keys():
                    if line.startswith(key):
                        reverse = key

                if reverse != None:
                    completions = \
                        [ opt for opt in self.__allowed_values[reverse] \
                            if opt.startswith(line) ]

            for completion in completions:
                yield Completion(text = completion,
                    start_position = -len(line),
                    display = completion,
                    display_meta = completion)

        except Exception as e:
            print(f"Exception {e}")

class clmapi_shell():
    __instance = None
    file = None
    __connected = False
    __evt = threading.Event()
    __c = None
    __RESPONSE_TIMEOUT = 5
    __option_completion = []
    __option_completion_allowed_values = {}
    __dynamic_group_completion = []
    __dynamic_group_names = []
    __config_export = None
    __expect_disconnect = False
    __address = None
    __password = None
    __nested_completer = None
    __completions = {}
    __fw_update_in_progress = False
    __fw_image_transfer_percent = 0
    __method_list = [ "help" ]
    __net_if_cache = { clmapi_pb2.NETWORK_TYPE_MODEM : { "icon": "?" },
                       clmapi_pb2.NETWORK_TYPE_ETHERNET : { "icon": "?" },
                       clmapi_pb2.NETWORK_TYPE_WIFI : { "icon": "?" },
                       clmapi_pb2.NETWORK_TYPE_BRIDGE : { "icon": "?" } }
    __status_message = "Type \"help\" to see the available commands."

    # make the class a singleton
    def __new__(cls, *args, **kwargs):
        if not clmapi_shell.__instance:
            clmapi_shell.__instance = object.__new__(cls)
            clmapi_shell.__setup_completions(clmapi_shell)
        return clmapi_shell.__instance

    def __setup_completions(self):
        method_list = [method[3:] for method in dir(self)
                            if method.startswith('do_') ]

        dlen = int((len(method_list)+2)/3)
        self.__method_list = list(zip(*[method_list[i:i+dlen]
                                for i in range(0, len(method_list), dlen)]))

        completions = {
            "help": dict.fromkeys(method_list, None)
        }

        for cmd in method_list:
            if (cmd != "help"):
                completions[cmd] = None

        group_list = clmapi_pb2.cfg_group.keys()
        if 'CFG_GROUP_RESERVED' in group_list:
            group_list.remove('CFG_GROUP_RESERVED')
        group_completion = WordCompleter(group_list)
        completions["cfg_get_group_info"] = group_completion
        completions["cfg_get_by_group"] = group_completion
        completions["cfg_export_by_group"] = group_completion
        completions["cfg_remove_group"] = \
            WordCompleter(self.__dynamic_group_names, sentence = True)
        completions["cfg_create_group"] = \
            WordCompleter(self.__dynamic_group_completion, sentence = True)
        completions["cfg_get_by_option"] = \
            OptionCompleter(self.__option_completion)
        completions["cfg_export_by_option"] = \
            OptionCompleter(self.__option_completion)
        completions["cfg_apply"] = \
            ApplyOptionCompleter(
                [opt + ("=\"" if opt.startswith("CFG_OPTION_STR_") else "=") \
                    for opt in self.__option_completion], \
                self.__option_completion_allowed_values)
        completions["cfg_validate"] = \
            ApplyOptionCompleter(
                [opt + ("=\"" if opt.startswith("CFG_OPTION_STR_") else "=") \
                    for opt in self.__option_completion], \
                self.__option_completion_allowed_values)
        completions["get_state"] = WordCompleter([member.name for member in clmlib.clm10k.STATE_REQUEST], sentence = False)
        completions["set_power_state"] = WordCompleter(
            [member for member in clmapi_pb2.power_command_type.keys() if "RESERVED" not in member], sentence = False)

        completions["ssh_copy_id"] = PathCompleter(expanduser = True)

        def extension_filter(file, ext):
            if os.path.isdir(file):
                return True

            if file.endswith(ext):
                return True

            return False


        def swu_filter(file):
            return extension_filter(file, ".swu")

        def clm_filter(file):
            return extension_filter(file, ".clm")

        completions["firmware_update"] = PathCompleter(expanduser = True,
                                                       file_filter = swu_filter)

        completions["cfg_import"] = PathCompleter(expanduser = True,
                                                  file_filter = clm_filter)

        completions["cfg_dump"] = PathCompleter(expanduser = True,
                                                  file_filter = clm_filter)

        completions["upload_file"] = PathCompleter(expanduser = True)


        self.__nested_completer = NestedCompleter.from_nested_dict(completions)

    def __prepare_option_completion_response(self, data):
        if (isinstance(data, clmapi_pb2.get_config_response)):
            options = []
            allowed_values = {}
            names = []
            for el in data.element:
                name = clmapi_pb2.cfg_option.Name(el.option)
                if (el.group_name != None) and (len(el.group_name) > 0):
                    name += f":{el.group_name}:"
                    names.append(el.group_name)

                if len(el.allowed_values) > 0:
                    name_key = name
                    if name.startswith("CFG_OPTION_STR_"):
                        name_key += "=\""
                    else:
                        name_key += "="

                    allowed_values[name_key] = [ \
                        f"{name}=\"{getattr(item, item.WhichOneof('val'))}\" " \
                        for item in el.allowed_values ]

                options.append(name)

            self.__option_completion = options
            self.__option_completion_allowed_values = allowed_values
            self.__dynamic_group_names = sorted(set(names))

        self.__evt.set()

    # called by connect, so self.__c is already initialized
    def __prepare_option_completion(self):
        self.__evt.clear()
        self.__c.get_config(with_description = False,
            cb_response = self.__prepare_option_completion_response)
        self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT)

    def __find_dynamic_group(self, data, out_groups):
        if (len(data.children) > 0):
            for child in data.children:
                self.__find_dynamic_group(child, out_groups)

        if (data.dynamic == True):
            out_groups.append(clmapi_pb2.cfg_group.Name(data.group_type))

    def __prepare_dynamic_group_completion_response(self, data):
        if (isinstance(data, clmapi_pb2.get_config_group_info_response)):
            groups = []
            for el in data.element:
                self.__find_dynamic_group(el, groups)

            self.__dynamic_group_completion = groups

        self.__evt.set()

    def __prepare_dynamic_group_completion(self):
        self.__evt.clear()
        self.__c.get_config_group_info(with_description = False,
            cb_response = self.__prepare_dynamic_group_completion_response)
        self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT)

    def __fill_net_if_cache_from_query(self, data):
        self.__evt.set()
        if (isinstance(data, clmapi_pb2.get_state_response)):
            for net in data.network:
                self.__cache_net_if_state(net.type,
                        net.interface.current_state.state)

    def __query_interfaces(self):
        self.__evt.clear()
        self.__c.get_state( request_states = [ clmlib.clm10k.STATE_REQUEST.NETWORK ],
                cb_response = self.__fill_net_if_cache_from_query)
        self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT)

    def __cb_fw_or_config_update(self, data):
        print(f"\n{data}")

    def __cb_connected(self, data):
        self.__connected = True
        self.__evt.set()

    def __cb_disconnected(self):
        self.__connected = False
        self.__evt.set()
        if (self.__expect_disconnect != True):
            self.__status_message = "Connection lost."
        else:
            self.__status_message = "Disconnected from the unit."

        self.__net_if_cache = { \
            clmapi_pb2.NETWORK_TYPE_MODEM : { "icon": "?" },
            clmapi_pb2.NETWORK_TYPE_ETHERNET : { "icon": "?" },
            clmapi_pb2.NETWORK_TYPE_WIFI : { "icon": "?" },
            clmapi_pb2.NETWORK_TYPE_BRIDGE : { "icon": "?" } }

    def __print_message(sef, data):
        print(data)

    def __generic_print_response(self, data):
        self.__print_message(data)
        self.__evt.set()

    def __parse_get_datetime_response(self, data):
        if (isinstance(data, clmapi_pb2.get_datetime_response)):
            dt = datetime.datetime.fromtimestamp(data.utc_seconds_from_epoch, \
                        datetime.timezone.utc)
            print(dt.astimezone())

        else: 
            print(data)
        self.__evt.set()

    def get_completer(self, text):
        return self.__nested_completer

    def help(self):
        print("Documented commands (type help <command>):")
        print("==========================================")
        print(tabulate(self.__method_list, tablefmt = "plain"))
        print()
        # print("Network interface state symbols:")
        # print("? - State of the network interface is not known.")
        # print("✔ - The network interface is configured and active.")
        # print("⧖ - The network interface is being activated.")
        # print("💔- An error has occured when activating the network interface.")
        # print("⊝ - The network interface is disconnected.")
        # print("⨯ - The network interface was disabled by the user.")

    def help_connect(self):
        print(
"""
Connect to the clm10k unit.

    connect [ip] [password]

Parameters
----------
    ip          IP address of the CLM10k unit
    password    API/WebUI admin password of the CLM10k unit
""")

    def do_connect(self, arg = ""):
        try:
            if ((arg == None) or (len(arg) == 0)):
                print("connect: missing parameters")
                return
            args = arg.split()

            if (len(args) < 2):
                print("connect: missing password")
                return

            if (len(args[0]) == 0):
                print("connect: missing address argument")
                return
            else:
                self.__address = args[0]

            if (len(args[1]) == 0):
                print("connect: missing password argument")
                return
            else:
                self.__password = args[1]

            self.__disconnect()

            self.__status_message = f"Connecting to {self.__address}..."
            self.__evt.clear()
            self.__expect_disconnect = True
            self.__c = clmlib.clm10k(self.__address, self.__password,
                    cb_firmware_update = self.__cb_fw_or_config_update,
                    cb_config_update = self.__cb_fw_or_config_update,
                    cb_connected = self.__cb_connected,
                    cb_disconnected = self.__cb_disconnected,
                    cb_if_state = self.__process_net_if_state_notification)

            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT) != True):
                self.__status_message = f"connect: failed to establish connection to {self.__address}"
                self.__disconnect()
                return

            if (self.__connected != True):
                self.__status_message = f"connect: failed to establish connection to {self.__address}"
                self.__disconnect()
                return

            self.__expect_disconnect = False
            self.__prepare_option_completion()
            self.__prepare_dynamic_group_completion()
            self.__setup_completions()
            self.__query_interfaces()
            self.__status_message = f"Connection to {self.__address} established."

        except Exception as e:
            print(f"Error: {e}")

    def __help_apply_or_validate(self, name):
        print(\
f"""
Apply one or more configuration options.

    cfg_{name} [option[:group_name:]=value] [option[:group_name:]=value] ...

Parameters
----------
    option  Configuration option to {name} (press TAB to list options)
            If the option is a member of a dynamic group, then the group
            name must be passed as well. This can be done by appending
            the group name surrounded by a colon:

            The value of the option is appended via the equals sign. String
            or byte values need to be enclosed in double quotes.
            For byte values, the input string will be converted to bytes,
            assuming UTF-8 input encoding.

            Boolean values must be entered numerically, i.e. 0 for False and
            1 for True.

            Example: 

            cfg_{name} CFG_OPTION_BOOL_CAN3_ENABLED=0 CFG_OPTION_STR_IF_ETHERNET_IPV4_METHOD:Wired connection:="link-local"
""")

    def __apply_or_validate(self, name, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print(f"cfg_{name}: not connected to the unit")
                return

            pb_options = []

            if (arg == None) or (len(arg) == 0):
                print(f"cfg_{name}: missing options and values to {name}")
                return

            group_name = pp.Combine(
                    pp.QuotedString(quote_char = ":", esc_char = "\\"))
            opt_id = (pp.Word(pp.alphanums + "_"))
            num_value = (pp.Optional('-') + pp.Word(pp.nums) + \
                         pp.Optional("." + pp.Word(pp.nums)))

            value = pp.Suppress("=") + \
                pp.Combine(
                        pp.QuotedString(quote_char = "\"", esc_char = "\\") | \
                        num_value)
            opt_id_with_value = opt_id + value
            opt_id_with_group_and_value = opt_id + pp.Optional(group_name) + \
                                          pp.Group(value)
            options = pp.OneOrMore(pp.Group(opt_id_with_group_and_value) | \
                      pp.Group(opt_id_with_value))
    
            reload_completions = False

            try:
                for option in options.parseString(arg):
                    try:
                        if len(option[-1]) != 1:
                            print(\
                                f"cfg_{name}: missing value for option {option[0]}")
                            return

                        val = option[-1][0]

                        # byte options require special handling, unfortunately
                        # we can not use a "fit all" approach here, because
                        # the BSSID needs to be converted from a string of
                        # of hex numbers to the actual hex values, i.e.
                        # you can not apply the BSSID as "001E42355F57",
                        # it has to be: 0x00 0x1E 0x42 0x35 0x35 0x5F 0x57
                        #
                        # we have only two BYTES options at the moment, so
                        # we will handle both of them in a user friendly
                        # way here
                        if (clmapi_pb2.cfg_option.Value(option[0]) == \
                            clmapi_pb2.cfg_option.CFG_OPTION_BYTES_IF_WIFI_BSSID) or \
                            (clmapi_pb2.cfg_option.Value(option[0]) == \
                            clmapi_pb2.cfg_option.CFG_OPTION_BYTES_IF_BRIDGE_WIFI_BSSID):
                            # allow in input, but drop colons
                            val = val.replace(":", "")
                            try:
                                val = bytes(bytearray.fromhex(val))
                            except ValueError:
                                raise ValueError("invalid BSSID value")
                        elif (clmapi_pb2.cfg_option.Value(option[0]) == \
                            clmapi_pb2.cfg_option.CFG_OPTION_BYTES_IF_WIFI_SSID) or \
                            (clmapi_pb2.cfg_option.Value(option[0]) == \
                            clmapi_pb2.cfg_option.CFG_OPTION_BYTES_IF_BRIDGE_WIFI_SSID):
                            # this covers most of the SSID cases
                            val = bytes(str(val), encoding = "UTF-8")
                        elif (clmapi_pb2.cfg_option.Value(option[0]) == \
                            clmapi_pb2.cfg_option.CFG_OPTION_STR_IF_MODEM_CONNECTION_NAME) or \
                            (clmapi_pb2.cfg_option.Value(option[0]) == \
                            clmapi_pb2.cfg_option.CFG_OPTION_STR_IF_ETHERNET_CONNECTION_NAME) or \
                            (clmapi_pb2.cfg_option.Value(option[0]) == \
                             clmapi_pb2.cfg_option.CFG_OPTION_STR_IF_WIFI_CONNECTION_NAME) or \
                            (clmapi_pb2.cfg_option.Value(option[0]) == \
                             clmapi_pb2.cfg_option.CFG_OPTION_BOOL_IF_BRIDGE_ENABLED):
                                reload_completions = True

                        # with group name
                        if len(option) == 3:
                            pb_options.append(
                                (clmapi_pb2.cfg_option.Value(option[0]), val, \
                                option[1]))
                        else:
                            pb_options.append(
                                (clmapi_pb2.cfg_option.Value(option[0]), val))
                    except ValueError as v:
                        raise ValueError(
                                f"error applying option {option[0]}: {v}")

            # the pyparsing exception is too crypting for the end user
            except pp.exceptions.ParseException:
                print(f"cfg_{name}: invalid input")
                return

            except Exception as e:
                print(f"cfg_{name}: invalid input: {e} ")
                return

            self.__evt.clear()

            if (name == "apply"):
                self.__c.apply_config(options = pb_options,
                                cb_response = self.__generic_print_response)
            else:
                self.__c.validate_config(options = pb_options,
                                cb_response = self.__generic_print_response)

            # increase timeout, some configurations may take a lot longer to
            # apply
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT * 3) != True):
                print(f"cfg_{name}: timed out")

            if (name == "apply") and reload_completions == True:
                self.__prepare_option_completion()
                self.__setup_completions()

        except Exception as e:
            print(f"cfg_{name}: error processing configuration: {e}")

    def help_cfg_apply(self):
        self.__help_apply_or_validate("apply")

    def do_cfg_apply(self, arg = ""):
        self.__apply_or_validate("apply", arg)

    def help_cfg_validate(self):
        self.__help_apply_or_validate("validate")

    def do_cfg_validate(self, arg = ""):
        self.__apply_or_validate("validate", arg)

    def help_cfg_get_by_option(self):
        print(
"""
Get one or more configuration options.

    cfg_get_by_option [option:group name:] [option] ...

Parameters
----------
    option  Configuration option to retrieve (press TAB to list options)
            If the option is a member of a dynamic group, then the group
            name must be passed as well. This can be done by appending
            the group name surrounded by a colon
            i.e.: CFG_OPTION_STR_IF_MODEM_APN:Mobile connection:

            If the option parameter is omitted, then all options will be
            returned.
""")

    def do_cfg_get_by_option(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("cfg_get_by_option: not connected to the unit")
                return

            pb_options = []

            if (arg != None) and (len(arg) > 0):
                group_name = pp.Combine(
                        pp.QuotedString(quote_char = ":", esc_char = "\\"))
                opt_id = (pp.Word(pp.alphanums + "_"))
                opt_id_with_group = opt_id + pp.Optional(group_name)
                options = pp.ZeroOrMore(
                        pp.Group(opt_id_with_group) | pp.Group(opt_id))

                try:
                    for option in options.parseString(arg):
                        if (len(option) > 1):
                            pb_options.append(
                                (clmapi_pb2.cfg_option.Value(option[0]),
                                 option[1]))
                        else:
                            pb_options.append(
                                    clmapi_pb2.cfg_option.Value(option[0]))
                except Exception:
                    print(f"cfg_get_by_option: invalid option {option}")
                    return

            self.__evt.clear()
            self.__c.get_config(options = pb_options,
                            cb_response = self.__generic_print_response)
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT) != True):
                print(f"cfg_get_by_option: timed out")

        except Exception as e:
            print(f"cfg_get_by_option: failed to retrieve options: {e}")

    def help_cfg_get_by_group(self):
        print(
"""
Get one or more configuration options which belong to given groups.

    cfg_get_by_group [group] [group] ...

Parameters
----------
    group   Configuration group to retrieve the options from (press TAB to 
            list available groups)

            If the group parameter is omitted, then options from all groups
            will be returned.
""")

    def do_cfg_get_by_group(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("cfg_get_by_group: not connected")
                return

            groups = []
            if (arg != None):
                groups = arg.split()

            pb_groups = []
            for group in groups:
                pb_groups.append(clmapi_pb2.cfg_group.Value(group))
            self.__evt.clear()
            self.__c.get_config(groups = pb_groups,
                            cb_response = self.__generic_print_response)
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT) != True):
                print(f"cfg_get_by_group: timed out")

        except Exception as e:
            print(f"cfg_get_by_group: failed to retrieve options: {e}")

    def help_cfg_get_group_info(self):
        print(
"""
Retrieve information about given groups.

    cfg_get_group_info [group] [group] ...

Parameters
----------
    group   Configuration group to retrieve the information for (press TAB to 
            list available groups)

            If the group parameter is omitted, then information about all groups
            will be returned.
""")


    def do_cfg_get_group_info(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("cfg_get_group_info: not connected")
                return

            groups = []
            if (arg != None):
                groups = arg.split()

            pb_groups = []
            for group in groups:
                pb_groups.append(clmapi_pb2.cfg_group.Value(group))
            self.__evt.clear()
            self.__c.get_config_group_info(groups = pb_groups,
                            cb_response = self.__generic_print_response)
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT) != True):
                print(f"cfg_get_group_info: timed out")

        except Exception as e:
            print(f"cfg_get_group_info: failed to retrieve group info: {e}")

    def help_cfg_create_group(self):
        print(
"""
Create a new configuration group, typically a network profile.

    cfg_create_group [group] [name]

Parameters
----------
    group   Type of the new configuration group (press TAB for a list of 
            available groups). Only dynamic groups are allowed here.

    name    Name of the new group.
""")


    def do_cfg_create_group(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("cfg_create_group: not connected")
                return

            if (arg == None) or (len(arg) == 0):
                print("cfg_create_group: missing parameters")
                return


            params = groups = arg.split(" ", 1)
            if (len(params) < 2) or (len(params[1]) == 0):
                print("cfg_create_group: missing group name")
                return

            group = clmapi_pb2.cfg_group.Value(params[0])
            name = params[1].strip()

            self.__evt.clear()
            self.__c.create_config_group(group, name,
                            cb_response = self.__generic_print_response)
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT) != True):
                print(f"cfg_create_group: timed out")
            else:
                # update group names
                self.__prepare_option_completion()
                self.__setup_completions()

        except Exception as e:
            print(f"cfg_create_group: failed to create group: {e}")

    def help_cfg_remove_group(self):
        print(
"""
Remove a configuration group, typically a network profile.

    cfg_remove_group [name]

Parameters
----------
    name    Name of the group which needs to be removed.
""")


    def do_cfg_remove_group(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("cfg_remove_group: not connected")
                return

            if (arg == None) or (len(arg) == 0):
                print("cfg_remove_group: missing parameters")
                return
            
            self.__evt.clear()
            self.__c.remove_config_group(arg.strip(),
                            cb_response = self.__generic_print_response)
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT) != True):
                print(f"cfg_remove_group: timed out")
            else: 
                # update group names
                self.__prepare_option_completion()
                self.__setup_completions()

        except Exception as e:
            print(f"cfg_remove_group: failed to remove group: {e}")

    def help_cfg_export_by_option(self):
        print(
"""
Export one or more configuration options. The export will be stored in
a temporary buffer which will be overwritten by each new export command.

To save the last export to a file see "cfg_save".

    cfg_export_by_option [option:group name:] [option] ...

Parameters
----------
    option  Configuration option to export (press TAB to list options)
            If the option is a member of a dynamic group, then the group
            name must be passed as well. This can be done by appending
            the group name surrounded by a colon
            i.e.: CFG_OPTION_STR_IF_MODEM_APN:Mobile connection:

            If the option parameter is omitted, then all options will be
            exported.
""")

    def __config_export_response(self, data):
        if (isinstance(data, clmapi_pb2.config_export_response)):
            self.__config_export = data.file_data 
            print("\nCaching configuration export, use: cfg_save filename to save it.\n")
        else:
            print(data)
        self.__evt.set()

    def do_cfg_export_by_option(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("cfg_export_by_option: not connected to the unit")
                return

            pb_options = []

            if (arg != None) and (len(arg) > 0):
                group_name = pp.Combine(
                        pp.QuotedString(quote_char = ":", esc_char = "\\"))
                opt_id = (pp.Word(pp.alphanums + "_"))
                opt_id_with_group = opt_id + pp.Optional(group_name)
                options = pp.ZeroOrMore(
                        pp.Group(opt_id_with_group) | pp.Group(opt_id))

                try:
                    for option in options.parseString(arg):
                        if (len(option) > 1):
                            pb_options.append(
                                (clmapi_pb2.cfg_option.Value(option[0]),
                                 option[1]))
                        else:
                            pb_options.append(
                                    clmapi_pb2.cfg_option.Value(option[0]))
                except Exception:
                    print(f"cfg_export_by_option: invalid option {option}")
                    return
     
            self.__evt.clear()
            self.__c.config_export(options = pb_options,
                            cb_response = self.__config_export_response)
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT) != True):
                print(f"cfg_export_by_option: timed out")

        except Exception as e:
            print(f"cfg_export_by_option: failed to export options: {e}")

    def help_cfg_export_by_group(self):
        print(
"""
Export one or more configuration options which belong to given groups.
The export will be stored in a temporary buffer which will be overwritten by
each new export command.

To save the last export to a file see "cfg_save".

    cfg_export_by_group [group] [group] ...

Parameters
----------
    group   Configuration group to export the options from (press TAB to 
            list available groups)

            If the group parameter is omitted, then options from all groups
            will be exported.
""")

    def do_cfg_export_by_group(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("cfg_export_by_group: not connected")
                return

            groups = []
            if (arg != None):
                groups = arg.split()

            pb_groups = []
            for group in groups:
                pb_groups.append(clmapi_pb2.cfg_group.Value(group))
            self.__evt.clear()
            self.__c.config_export(groups = pb_groups,
                            cb_response = self.__config_export_response)
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT) != True):
                print(f"cfg_export_by_group: timed out")

        except Exception as e:
            print(f"cfg_export_by_group: failed to export options: {e}")

    def help_cfg_export_factory_defaults(self):
        print(
"""
Export a configuration file with a reset to factory defaults instruction.
The export will be stored in a temporary buffer which will be overwritten by
each new export command.

To save the last export to a file see "cfg_save".

    cfg_export_by_group [group] [group] ...

Parameters
----------
    none
""")

    def do_cfg_export_factory_defaults(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("cfg_export_factory_defaults: not connected")
                return

            self.__evt.clear()
            self.__c.config_export(factory_reset = True,
                            cb_response = self.__config_export_response)
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT) != True):
                print(f"cfg_export_factory_defaults: timed out")

        except Exception as e:
            print(f"cfg_export_factory_defaults: failed to export options: {e}")

    def help_cfg_save(self):
        print(
"""
Save configuration export which has been previously cached by
cfg_export_by_option or cfg_export_by_group. After saving the configuration
the cache will be reset.

    cfg_save [filename]

Parameters
----------
    filename    Name of the file where to save the configuration export to,
                the file extension should be chosen.
""")

    def do_cfg_save(self, arg = ""):
        try:
            if (arg == None) or (len(arg) == 0):
                print("cfg_save: missing filename parameter")
                return

            if (self.__config_export == None):
                print("No configuration export found, please run cfg_export_by_option or cfg_export_by_group first.")
                return

            f = open(arg, "wb")
            f.write(self.__config_export)
            f.close()

            print(f"Configuration export saved as {arg}")
            self.__config_export = None

        except Exception as e:
            print(f"cfg_save: failed to save configuration: {e}")

    def help_reset_to_factory_settings(self):
        print(
"""
Reset device to factory settings.

The factory reset deletes all data on the device and restores
the original factory settings.
""")

    def do_reset_to_factory_settings(self, arg = ""):
        self.__expect_disconnect = True
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("reset_to_factory_settings: not connected to the unit")
                return

            print("Device will now reboot, please wait and reconnect manually.")
            self.__evt.clear()
            self.__c.reset_to_factory_settings(
                            cb_response = self.__generic_print_response)
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT*2) != True):
                print(f"reset_to_factory_settings: timed out")

        except Exception as e:
            print(f"reset_to_factory_settings: failed to reset device: {e}")

    def help_reset_configuration(self):
        print(
"""
Reset device configuration to factory defaults.
Note, that the configuration reset only restores the application
settings to factory defaults and does not affect credentials
nor removes created users.
""")

    def do_reset_configuration(self, arg = ""):
        self.__expect_disconnect = True
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("reset_configuration: not connected to the unit")
                return

            print("Please wait, application will be restarted.")
            self.__evt.clear()
            self.__c.reset_configuration(
                            cb_response = self.__generic_print_response)
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT*2) != True):
                print(f"reset_configuration: timed out")
            else:
                if (not self.__connected):
                    time.sleep(5)
                    self.do_reconnect("")

        except Exception as e:
            print(f"reset_configuration: failed to reset configuration: {e}")
 
    def help_firmware_update_abort(self):
        print(
"""
Abort an ongoing firmware update download. Once flashing has started, the
update can no longer be aborted. The function will not fail even if there
is no ongoing update.

    abort_firmware_update

Parameters
----------
    none
""")

    def do_firmware_update_abort(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("firmware_update_abort: not connected")
                return

            self.__evt.clear()
            self.__c.firmware_update_abort(
                            cb_response = self.__generic_print_response)
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT) != True):
                print("firmware_update_abort: timed out")

        except Exception as e:
            print(f"firmware_update_abort: failed to abort update: {e}")

    def help_get_device_info(self):
        print(
"""
Retrieve device information.

    get_device_info

Parameters
----------
    none
""")

    def do_get_device_info(self, arg = ""):
        if ((self.__c == None) or (not self.__connected)):
           print("get_device_info: not connected")
           return

        print(self.__c.get_device_info())

    def help_set_password(self):
        print(
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
    username    Name of the user for which the password needs to be set. 
                Currently either "admin" or "upload".

    admin_pass  Current admin password which is required for the validation of
                this call.

    new_pass    New password for the selected user.
""")

    def do_set_password(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("set_password: not connected")
                return

            if (arg == None) or (len(arg) == 0):
                print("set_password: missing parameters")
                return
 
            params = arg.split()
            if (len(params) != 3):
                print("set_password: missing parameters")
                return

            username = params[0]
            if (username != "admin") and (username != "upload"):
                print("set_password: invalid user name")
                return

            admin_pass = params[1]
            new_pass = params[2]
 
            self.__evt.clear()
            self.__c.set_password(username, admin_pass, new_pass,
                            cb_response = self.__generic_print_response)
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT) != True):
                print("set_password: timed out")

        except Exception as e:
            print(f"set_password: failed to set password: {e}")

    def help_remove_upload_user(self):
        print(
"""
Remove the "upload" user. This function will not fail if the user does not
exist.

Parameters
----------
    none
""")

    def do_remove_upload_user(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("remove_upload_user: not connected")
                return

            self.__evt.clear()
            self.__c.remove_upload_user(
                            cb_response = self.__generic_print_response)
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT) != True):
                print("remove_upload_user: timed out")

        except Exception as e:
            print(f"remove_upload_user: failed to remove user: {e}")

    def help_get_users(self):
        print(
"""
Get a list of user accounts which are currently available on the system.

Parameters
----------
    none
""")

    def do_get_users(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("get_users_request: not connected")
                return

            self.__evt.clear()
            self.__c.get_users(
                            cb_response = self.__generic_print_response)
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT) != True):
                print("get_users_request: timed out")

        except Exception as e:
            print(f"get_users_request: failed to retrieve list of users: {e}")


    def help_set_datetime(self):
        print(
"""
Set date and time on the device.

Parameters
----------
    date string String representing the desired timestamp. Various formats
                are supported, for instance "2022-05-28 22:29:05" or
                "May 28, 2022, 22:29:05" will work as well. To use the
                current timestamp of the system where clmshell has been
                launched use the keyword "now" instead of a date/time string.

                Also note, that if you do not specify a timezone explicitly
                clmshell will assume that your timestamp is in your local
                time format.
""")

    def do_set_datetime(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("set_datetime: not connected to the unit")
                return

            if (arg == None) or (len(arg) == 0):
                print("set_datetime: missing date/time string")
                return

            ts = None

            if (arg == "now"):
                ts = datetime.datetime.now()
            else:
                ts = parser.parse(arg)

            self.__evt.clear()
            self.__c.set_datetime(timestamp = ts,
                            cb_response = self.__generic_print_response)
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT) != True):
                print("set_datetime: timed out")

        except Exception as e:
            print(f"set_datetime: failed to set date and time: {e}")

    def help_get_datetime(self):
        print(
"""
Get date and time on the device.

Note: the device will deliver the time in UTC, clmshell will convert the device
      time to your local time.

""")

    def do_get_datetime(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("get_datetime: not connected to the unit")
                return

            self.__evt.clear()
            self.__c.get_datetime(
                            cb_response = self.__parse_get_datetime_response)
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT) != True):
                print("get_datetime: timed out")

        except Exception as e:
            print(f"get_datetime: failed go set date and time: {e}")

    def help_get_state(self):
        print(
"""
Get volatile device states.

This will return various device states, like the actual network confguration
and data portal connectivity.
""")

    def do_get_state(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("get_state: not connected to the unit")
                return

            states = []
            if (arg != None):
                states = arg.split()

            # remove duplicates
            states = list(set(states))
            states = [ clmlib.clm10k.STATE_REQUEST[member] for member in states ]

            self.__evt.clear()
            self.__c.get_state(request_states = states,
                            cb_response = self.__generic_print_response)
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT) != True):
                print("get_state: timed out")

        except Exception as e:
            print(f"get_state: failed to request state from the device: {e}")

    def help_ssh_copy_id(self):
        print(
"""
Install your public ssh key on the device to enable root login access via ssh.

This is similar to the ssh-copy-id on Linux, with the difference that you
need to specify which key to transfer as a parameter.

Parameters
----------
    public_key_file: file containing your public ssh key which you would like
                     to use for public key authentication, typically found
                     in ~/.ssh/ on Linux systems
""")

    def do_ssh_copy_id(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("ssh_copy_id: not connected")
                return

            arg = os.path.expanduser(arg.strip())

            # This function uses the http API and is synchronous
            self.__c.ssh_copy_id(arg)
            print(f"Key {arg} has been installed on the device, root login\nwill be available for the duration of 2 hours.")

        except Exception as e:
            print(f"ssh_copy_id: failed to install public key on the device {e}")

    def help_firmware_update(self):
        print(
"""
Update device firmware with the given firmware image.

The image must be an '.swu' update image.

Parameters
----------
    firmware_image_file: .swu firmware image
""")

    def __print_fw_transfer_progress(self, data):
        try:
            if data.firmware_update.transfer_progress_percent != None:
                if (data.firmware_update.transfer_progress_percent <= \
                        self.__fw_image_transfer_percent) and \
                            (self.__fw_image_transfer_percent != 0):
                    return

                self.__fw_image_transfer_percent = \
                                data.firmware_update.transfer_progress_percent
                print(f"{self.__fw_image_transfer_percent}%...",
                      flush = True, end = "")

        except Exception as e:
            print(f"Progress error: {e}")


    def do_firmware_update(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("firmware_update: not connected")
                return

            if self.__fw_update_in_progress == True:
                print("firmware_update: another update is currently in progress")
                return

            # if the upload thread goes awol, ensure we do not allow to start
            # yet another update
            self.__fw_update_in_progress = True
            self.__fw_image_transfer_percent = 0

            arg = os.path.expanduser(arg.strip())

            evt = threading.Event()
            evt.clear()

            aborted = False

            def transfer_firmware(self):
                try:
                    # This function uses the http API and is synchronous, we
                    # however would like to query the state from the device
                    print("Transferring firmare image to the device...")
                    self.__c.firmware_update(arg)
                    evt.set()
                    if not aborted and self.__fw_image_transfer_percent < 100:
                        print("100%")

                except Exception as e:
                    print(e)
                    evt.set()

            t = threading.Thread(target = transfer_firmware, args = [ self ])
            t.start()

            while not evt.is_set():
                if self.__connected:
                    self.__c.get_state(
                        request_states =
                            [ clmlib.clm10k.STATE_REQUEST.FIRMWARE_UPDATE ],
                        cb_response = self.__print_fw_transfer_progress)
                evt.wait(timeout = 5) # update progress every 5 seconds

            t.join()

        except KeyboardInterrupt:
            print("\nAborting image transfer, please wait...")
            aborted = True
            if self.__connected:
                self.__c.firmware_update_abort()
            evt.set()
            t.join()
            print()

        except Exception as e:
            print(f"firmware_update: {e}")

        finally:
            self.__fw_update_in_progress = False
            self.__fw_image_transfer_percent = 0

    def help_cfg_import(self):
        print(
"""
Import configuration.

Import a configuration file which has been previously exported using the
cfg_export_*/cfg_save commands.

Parameters
----------
    config_file: config file in the clm10k configuration format
""")

    def do_cfg_import(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("cfg_import: not connected")
                return

            arg = os.path.expanduser(arg.strip())

            # This function uses the HTTP API and is synchronous
            self.__c.cfg_import(arg)
            print(f"Configuration {arg} has been transferred to the device.")

        except Exception as e:
            print(f"cfg_import: {e}")

    def help_cfg_dump(self):
        print(
"""
Decode a previously exported configuration file and show its contents.

Parameters
----------
    file: name of the configuration file
""")

    def do_cfg_dump(self, arg = ""):
        try:
            arg = os.path.expanduser(arg.strip())

            # This function is synchronous
            cfg = clmlib.clm10k.decode_config(arg)
            print(f"\nConfiguration dump of {arg}:\n{cfg}")

        except Exception as e:
            print(f"cfg_dump: {e}")

    def help_upload_file(self):
        print(
"""
Upload a file to the device for a further transfer to the DataPortal.

The uploaded file will be stored on the device and will further be
transferred to the DataPortal. Once transferred, the file will be removed
from the unit.

Note, that due to a limitation in the DataPortal the total size of the upload
must not exceed 1048576 bytes, including HTTP headers and encoding.

Parameters
----------
    file: name of the file to upload
""")

    def do_upload_file(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("upload_file: not connected")
                return

            arg = os.path.expanduser(arg.strip())

            # This function uses the HTTP API and is synchronous
            self.__c.upload_file(arg)
            print(f"File {arg} has been transferred to the device.")

        except KeyboardInterrupt:
            print("\nAborting file transfer, please wait...")

        except Exception as e:
            print(f"upload_file: {e}")

    def help_set_power_state(self):
        print(
"""
Set power state.

Parameters
----------
    type string String representing the desired power command.
                POWER_COMMAND_TYPE_SHUTDOWN turns the device off immediately,
                and the system remains off regardless of the wake-up configuration.
                This command is intended to be used in the testing environment,
                as the device will start again only on the power cycle of the Clamp30.
                POWER_COMMAND_TYPE_REBOOT type shuts down the system and then restarts
                immediately. POWER_COMMAND_TYPE_STANDBY puts the device into sleep mode
                when the Clamp15 goes low and wakes up on configured wake-up events.
""")

    def do_set_power_state(self, arg = ""):
        try:
            if ((self.__c == None) or (not self.__connected)):
                print("set_power_state: not connected")
                return

            power_command_type = clmapi_pb2.power_command_type.Value(arg)

            self.__evt.clear()
            self.__c.set_power_state(
                            power_command_type = power_command_type,
                            cb_response = self.__parse_get_datetime_response)
            if (self.__evt.wait(timeout = self.__RESPONSE_TIMEOUT) != True):
                print("set_power_state: timed out")

        except KeyboardInterrupt:
            print("\nAborting file transfer, please wait...")

        except Exception as e:
            print(f"set_power_state: {e}")

    def emptyline(self):
        pass

    def __disconnect(self):
        if (self.__c != None):
            self.__expect_disconnect = True
            self.__c.terminate()
            self.__c = None

    def do_disconnect(self, arg = ""):
        "Disconnect from the unit."
        self.__status_message = "Disconnecting, please wait..."
        self.__disconnect()
        self.__status_message = "Disconnected from the unit."

    def do_reconnect(self, arg = ""):
        "Reconnect to the last known address using last known credentials."
        if (self.__address != None) and (self.__password != None):
            self.do_connect(f"{self.__address} {self.__password}")
        else:
            print("No address and password cached, please use the connect command.")

    def help_quit(self):
        print("Exit application.")

    def exit(self):
        if (self.__connected):
            self.do_disconnect()

    def process(self, command):
        if ((command == None) or (len(command) == 0)):
            return

        sep = command.split(" ", 1)
        cmd = sep[0].strip()
        if ((cmd == None) or (len(cmd) == 0)):
            return

        cmd = cmd.lower()

        arg = ""
        if (len(sep) > 1):
            arg = sep[1]

        if (cmd == "quit"):
            raise EOFError

        try:
            if (cmd == "help") or (cmd == "?"):
                func = None
                if (arg != None) and (len(arg) > 0):
                    try:
                        func = getattr(self, f"help_{arg}")
                    except:
                        pass

                if (func != None):
                    func()
                else:
                    self.help()
            else:
                func = getattr(self, f"do_{cmd}")
                func(arg = arg) # except into "Unknown command"
        except:
            self.__status_message = f"Unknown command: {command}"
            return

    def __cache_net_if_state(self, net_type, state):
        if state == clmapi_pb2.NETWORK_IF_STATE_ACTIVATING:
            self.__net_if_cache[net_type]["icon"] = "&#x29D6; "
        elif state == clmapi_pb2.NETWORK_IF_STATE_ACTIVATED:
            self.__net_if_cache[net_type]["icon"] = "&#x2714;"
        elif state == clmapi_pb2.NETWORK_IF_STATE_FAILED:
            self.__net_if_cache[net_type]["icon"] = "&#x1F494; "
        elif state == clmapi_pb2.NETWORK_IF_STATE_DISCONNECTED:
            self.__net_if_cache[net_type]["icon"] = "&#x229D; "
        elif state == clmapi_pb2.NETWORK_IF_STATE_UNKNOWN:
            self.__net_if_cache[net_type]["icon"] = "?"
        elif state == clmapi_pb2.NETWORK_IF_STATE_DISABLED:
            self.__net_if_cache[net_type]["icon"] = "&#x02A2F; "
        else:
            self.__net_if_cache[net_type]["icon"] = " "

    def __process_net_if_state_notification(self, data):
        self.__cache_net_if_state(data.type, data.state.state)

    def toolbar(self):
        return HTML(f" Modem {self.__net_if_cache[clmapi_pb2.NETWORK_TYPE_MODEM]['icon']} | Ethernet {self.__net_if_cache[clmapi_pb2.NETWORK_TYPE_ETHERNET]['icon']} &#x2190;[{self.__net_if_cache[clmapi_pb2.NETWORK_TYPE_BRIDGE]['icon']}]&#x2192; WiFi {self.__net_if_cache[clmapi_pb2.NETWORK_TYPE_WIFI]['icon']} | {self.__status_message }")

class DynamicCompleter(Completer):
    def get_completions(self, document, complete_event):
        completer = clmapi_shell().get_completer(document)

        return completer.get_completions(document, complete_event)


def main():
    print('\nCopyright 2022-2023 Proemion GmbH.')
    print('clm10k proto API shell. Type help or ? for a list of commands.\n')

    session = PromptSession()
    shell = clmapi_shell()

    while True:
        try:
            command = session.prompt('clm10k> ',
                        completer = DynamicCompleter(),
                        color_depth = ColorDepth.DEPTH_1_BIT,
                        complete_style = CompleteStyle.READLINE_LIKE,
                        # bottom_toolbar = shell.toolbar,
                        refresh_interval = 2)
            shell.process(command)
        except KeyboardInterrupt:
            continue # Ctrl-C, abort input
        except EOFError:
            break # Ctrl-D / exit

    shell.exit()

if __name__ == '__main__':
    main()
