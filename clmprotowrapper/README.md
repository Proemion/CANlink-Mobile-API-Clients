# About
Wrapper for the protocol buffer API of the CANlink mobile 10000

# Installation

The latest built version can be installed from PyPI.
Install from PyPI using `pip`:

```
pip install clmprotowrapper
```

# Building
We use [poetry](https://python-poetry.org) as build system. 

### Build a version for a new version of the device API 
For this use-case you can replace the `clmapi.proto` with a new version and then run a new build. The latest version of 
`clmapi.proto` can be downloaded under https://www.proemion.com/download-center -> Devices. The version label of the 
firmware (which should is also the version ot the protobuf API) should match with the version of this package.

### Building custom versions
In case you need a custom version, e.g. because you might have a different proto API version running on your 
device you can replace the `clmapi.proto` with your custom version and then run a new build with poetry build.