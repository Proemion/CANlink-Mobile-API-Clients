'''
This build script uses the clmapi.proto to generate the clmapi_pb2.py module from the which is the wrapper
for the protobuf API. It also runs on poetry build.
'''

import os
from grpc_tools import protoc


def build(setup_kwargs):
    root = os.path.dirname(os.path.abspath(__file__))
    proto_file = os.path.join(root, "clmapi.proto")
    out_dir = os.path.join(root, "clmprotoclient")

    ret = protoc.main([
        "grpc_tools.protoc",
        f"-I{root}",
        f"--python_out={out_dir}",
        proto_file,
    ])

    if ret != 0:
        raise RuntimeError(f"protoc exited with code {ret}")


if __name__ == "__main__":
    build({})

