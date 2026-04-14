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
from setuptools import setup, find_packages
from setuptools.command.egg_info import egg_info

def read(fname):
    return open(os.path.join(os.path.dirname(__file__), fname)).read()

def get_package_version():
    version = os.getenv("PACKAGE_VERSION")
    if (version == None):
        version = "git"

    return version

def get_package_name():
    name = "clm10k-protomolecule"
    oem_variant = os.getenv("OEM_VARIANT")
    if (oem_variant != None):
        name += f"-${oem_variant}"

    return name

setup(
    name = get_package_name(),
    version = get_package_version(),
    description = ( "Python module which helps to communicate with the clm10k "
                    "via the protocol buffers based API."),
    long_description=read('README.md'),
    license = "Other/Proprietary License",
    package_dir = { "clmapi" : "clmapi" },
    packages = [ "clmapi" ],
    install_requires =
    [
        "websocket-client>=1.2.3",
        "protobuf>=3.14.0",
        "prompt-toolkit>=3.0.24",
        "python-dateutil>=2.8.1",
        "requests>=2.28.1",
        "pyparsing>=2.4.7",
        "tabulate>=0.8.9"
    ],
    classifiers =
    [
        "Programming Language :: Python :: 3",
        "License :: Other/Proprietary License",
        "Operating System :: OS Independent"
    ],
    entry_points = { "console_scripts": [ "clmshell = clmapi.clmshell:main" ] },
    options = { "build_scripts": { "executable" : "/usr/bin/env python3" } }
)
