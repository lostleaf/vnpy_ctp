import platform

from setuptools import setup
from pybind11.setup_helpers import Pybind11Extension, build_ext


def _ctp_extension(name: str, sources: list[str]) -> Pybind11Extension:
    if platform.system() == "Linux":
        return Pybind11Extension(
            name=name,
            sources=sources,
            include_dirs=["vnpy_ctp/api/include", "vnpy_ctp/api/vnctp"],
            library_dirs=["vnpy_ctp/api"],
            extra_compile_args=[
                "-std=c++17",
                "-O3",
                "-Wno-delete-incomplete",
                "-Wno-sign-compare",
            ],
            extra_link_args=["-lstdc++"],
            runtime_library_dirs=["$ORIGIN"],
            libraries=["thostmduserapi_se", "thosttraderapi_se"],
            language="cpp",
        )
    else:
        raise RuntimeError(f"Platform {platform.system()} is not supported")


setup(
    cmdclass={"build_ext": build_ext},
    ext_modules=[
        _ctp_extension(
            "vnpy_ctp.api.vnctpmd",
            ["vnpy_ctp/api/vnctp/vnctpmd/vnctpmd.cpp"],
        ),
        _ctp_extension(
            "vnpy_ctp.api.vnctptd",
            ["vnpy_ctp/api/vnctp/vnctptd/vnctptd.cpp"],
        ),
    ],
)
