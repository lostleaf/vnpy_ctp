# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

vnpy_ctp 是 VeighNa (vn.py) 量化交易框架的 CTP 期货接口网关，基于上期技术 CTP 期货版 6.7.11 API 封装

通过 pybind11 将 C++ CTP API 封装为 Python 扩展模块，提供行情（MdApi）和交易（TdApi）两套接口

## 包管理规范

- 本项目使用 **uv** 管理虚拟环境和依赖。所有依赖安装必须通过 `uv sync` 完成
- 需要新增依赖时，应将其添加到 `pyproject.toml` 的对应位置
- 除非需要编译项目 C/C++ 扩展, **禁止使用 `uv pip install`** 直接安装依赖

## 构建与安装

```bash
# 同步虚拟环境（安装所有依赖 + 以 editable 模式安装项目本身）
uv sync

# 编译 C++ 扩展（--no-build-isolation 复用 venv 中已安装的 setuptools/pybind11）
uv pip install -e . --no-build-isolation
```

构建系统使用 **setuptools + pybind11**，配置在 `setup.py` 和 `pyproject.toml`。

构建依赖：setuptools、pybind11 >= 2.13.6。

## 代码检查

```bash
# Lint（规则配置在 pyproject.toml [tool.ruff]）
ruff check .

# 类型检查（严格模式，配置在 pyproject.toml [tool.mypy]）
mypy vnpy_ctp
```

## 测试

测试位于 `test/` 目录，使用 pytest。注意：测试需要连接真实 CTP 服务器并配置有效账户信息，属于集成测试。

```bash
# 运行全部测试
pytest test/

# 运行单个测试
pytest test/test_md.py
pytest test/test_td.py::test_query_instrument
```

## 架构

### 分层结构

```
Python 策略层（用户代码）
    ↓
CtpGateway（vnpy_ctp/gateway/ctp_gateway.py）
    - CtpMdApi：行情接口封装，继承自 C++ MdApi
    - CtpTdApi：交易接口封装，继承自 C++ TdApi
    - 负责 CTP 数据结构 ↔ VeighNa 数据结构的映射转换
    ↓
pybind11 C++ 扩展模块（vnpy_ctp/api/）
    - vnctpmd：行情扩展（vnctpmd.cpp/h）
    - vnctptd：交易扩展（vnctptd.cpp/h）
    - vnctp.h：公共工具（任务队列、GBK↔UTF-8 编码转换）
    ↓
CTP 官方动态库
    - thostmduserapi_se（行情 API）
    - thosttraderapi_se（交易 API）
```

### 核心回调机制

C++ 层使用任务队列（TaskQueue）实现异步回调：
1. CTP 回调
2. 封装为 Task 推入线程安全队列
3. 工作线程取出并转换为 Python dict
4. 调用 Python 端对应的 `on*` 回调方法

编码转换在 C++ 层完成（GBK → UTF-8）

### 平台

仅支持 Linux：C++17，动态链接 .so，rpath 设置为 `$ORIGIN`

## 关键文件

| 文件 | 作用 |
|------|------|
| `vnpy_ctp/gateway/ctp_gateway.py` | Python 网关主逻辑，CTP ↔ VeighNa 数据映射 |
| `vnpy_ctp/api/vnctp/vnctptd/vnctptd.cpp` | 交易 API 的 C++ pybind11 封装 |
| `vnpy_ctp/api/vnctp/vnctpmd/vnctpmd.cpp` | 行情 API 的 C++ pybind11 封装 |
| `vnpy_ctp/api/vnctp/vnctp.h` | 公共工具：TaskQueue、编码转换、dict 访问 |
| `vnpy_ctp/api/ctp_constant.py` | 自动生成的 CTP 常量定义 |
| `setup.py` | C++ 扩展模块构建配置 |

## 开发注意事项

- 项目使用中文注释和中文 commit message（如 `[Mod] 优化接口代码中的类型声明`）
- `ctp_constant.py` 和 C++ 绑定代码由 `generator/` 脚本生成，修改时应修改生成器而非直接修改生成文件
- CTP API 使用 GBK 编码，所有字符串在 C++ 层转换为 UTF-8 后传给 Python
- 运行时依赖 vnpy >= 3.0.0 框架
