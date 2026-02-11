# CTP C++ Binding 机制文档

本文档详尽描述 vnpy_ctp 项目中 pybind11 封装 CTP C++ API 的完整机制，目标是让工程师和 AI agent **在尽可能不看现有 C++ binding 代码的情况下**，能够开发新的接口 binding。

---

## 目录

1. [总览](#1-总览)
2. [核心基础设施（vnctp.h）](#2-核心基础设施vnctph)
3. [CTP 数据类型映射规则](#3-ctp-数据类型映射规则)
4. [包装类的类结构](#4-包装类的类结构)
5. [回调处理的三阶段流水线](#5-回调处理的三阶段流水线)
6. [请求方法的代码模式](#6-请求方法的代码模式)
7. [PyXxxApi 的 pybind11 trampoline 类](#7-pyxxxapi-的-pybind11-trampoline-类)
8. [PYBIND11_MODULE 注册](#8-pybind11_module-注册)
9. [构建系统](#9-构建系统)
10. [新增接口的 Checklist](#10-新增接口的-checklist)

---

## 1. 总览

### 1.1 分层架构

```
Python 策略层（用户代码）
    │
    │  继承 MdApi/TdApi 并重写 on* 回调方法
    ↓
pybind11 包装类（MdApi / TdApi）
    │
    │  C++ 类继承 CTP 官方 Spi，封装请求方法和回调转发
    ↓
CTP 官方 C++ API（CThostFtdcMdApi / CThostFtdcTraderApi）
    │
    │  通过动态链接调用
    ↓
CTP 官方动态库（.so）
    - libthostmduserapi_se.so   （行情 API）
    - libthosttraderapi_se.so   （交易 API）
```

### 1.2 两套接口

| 接口 | 用途 | CTP 基类 | 包装类 | 模块名 |
|------|------|----------|--------|--------|
| MdApi | 行情（订阅/接收市场数据） | `CThostFtdcMdSpi` | `MdApi` | `vnctpmd` |
| TdApi | 交易（报单/查询/资金等） | `CThostFtdcTraderSpi` | `TdApi` | `vnctptd` |

两套接口的封装结构**完全对称**，差别仅在于方法数量（TdApi 远多于 MdApi）和具体的数据结构。

### 1.3 文件组织结构

```
vnpy_ctp/api/
├── include/ctp/                          # CTP 官方头文件
│   ├── ThostFtdcMdApi.h                  # 行情 API 接口定义
│   ├── ThostFtdcTraderApi.h              # 交易 API 接口定义
│   ├── ThostFtdcUserApiDataType.h        # 字段类型 typedef
│   └── ThostFtdcUserApiStruct.h          # 数据结构定义
├── vnctp/
│   ├── vnctp.h                           # 公共基础设施
│   ├── vnctpmd/
│   │   ├── vnctpmd.h                     # MdApi 包装类声明
│   │   └── vnctpmd.cpp                   # MdApi 实现 + PyMdApi + 模块注册
│   └── vnctptd/
│       ├── vnctptd.h                     # TdApi 包装类声明
│       └── vnctptd.cpp                   # TdApi 实现 + PyTdApi + 模块注册
├── libthostmduserapi_se.so               # CTP 行情动态库
├── libthosttraderapi_se.so               # CTP 交易动态库
├── ctp_constant.py                       # 自动生成的 CTP 常量
└── __init__.py
```

---

## 2. 核心基础设施（vnctp.h）

`vnctp.h` 是被 MdApi 和 TdApi 共同 `#include` 的头文件，提供以下公共组件。

### 2.1 Task 结构体

```cpp
struct Task
{
    int task_name;      // 回调函数名称对应的常数（#define 宏）
    void *task_data;    // 数据指针（CTP 数据结构的深拷贝，如 CThostFtdcOrderField*）
    void *task_error;   // 错误指针（CThostFtdcRspInfoField* 的深拷贝）
    int task_id;        // 请求 ID（nRequestID）或整型参数（如 nReason）
    bool task_last;     // 是否为最后一条响应（bIsLast）
};
```

**使用场景**：CTP 回调在 CTP 内部线程触发，不能直接操作 Python 对象。Task 作为中间载体，将回调数据从 CTP 线程安全地传递到工作线程。

各字段的使用方式取决于回调类型：
- **OnRsp\* 回调**：5 个字段全部使用
- **OnRtn\* 回调**：仅使用 `task_name` 和 `task_data`
- **OnErrRtn\* 回调**：使用 `task_name`、`task_data` 和 `task_error`
- **OnFrontConnected**：仅使用 `task_name`
- **OnFrontDisconnected / OnHeartBeatWarning**：使用 `task_name` 和 `task_id`

### 2.2 TaskQueue 类

```cpp
class TaskQueue
{
private:
    queue<Task> queue_;              // 标准队列
    mutex mutex_;                    // 锁
    condition_variable cond_;        // 条件变量
    bool _terminate = false;

public:
    // 存入新的任务（CTP 回调线程调用）
    void push(const Task &task)
    {
        unique_lock<mutex> mlock(mutex_);
        queue_.push(task);
        mlock.unlock();
        cond_.notify_one();          // 通知等待中的工作线程
    }

    // 取出最早的任务（工作线程调用，阻塞等待）
    Task pop()
    {
        unique_lock<mutex> mlock(mutex_);
        cond_.wait(mlock, [&]() {
            return !queue_.empty() || _terminate;
        });
        if (_terminate)
            throw TerminatedError();
        Task task = queue_.front();
        queue_.pop();
        return task;
    }

    // 终止队列（exit 时调用，唤醒所有等待线程）
    void terminate()
    {
        _terminate = true;
        cond_.notify_all();
    }
};
```

**机制**：生产者-消费者模式。CTP 回调线程通过 `push()` 将 Task 入队，工作线程通过 `pop()` 阻塞等待并取出 Task。`terminate()` 用于 API 退出时中断工作线程的等待。

### 2.3 TerminatedError

```cpp
class TerminatedError : std::exception
{};
```

当 `TaskQueue::terminate()` 被调用后，`pop()` 会抛出此异常，用于通知工作线程退出循环。工作线程的 `processTask()` 方法通过 `catch (const TerminatedError&)` 捕获并安静退出。

### 2.4 字典辅助函数

这四个函数用于**请求方法**中，从 Python `dict` 中提取值并赋给 CTP 结构体字段。它们的共同特征是：**如果键不存在则不修改目标值**（保持 `memset` 后的零值）。

#### getInt — 整数字段

```cpp
void getInt(const dict &d, const char *key, int *value)
{
    if (d.contains(key))
    {
        object o = d[key];
        *value = o.cast<int>();
    }
};
```

调用方式：`getInt(req, "VolumeTotalOriginal", &myreq.VolumeTotalOriginal);`

#### getDouble — 浮点数字段

```cpp
void getDouble(const dict &d, const char *key, double *value)
{
    if (d.contains(key))
    {
        object o = d[key];
        *value = o.cast<double>();
    }
};
```

调用方式：`getDouble(req, "LimitPrice", &myreq.LimitPrice);`

#### getChar — 单字符字段

```cpp
void getChar(const dict &d, const char *key, char *value)
{
    if (d.contains(key))
    {
        object o = d[key];
        *value = o.cast<char>();
    }
};
```

调用方式：`getChar(req, "Direction", &myreq.Direction);`

#### getString — 字符串字段

```cpp
template <size_t size>
using string_literal = char[size];

template <size_t size>
void getString(const pybind11::dict &d, const char *key, string_literal<size> &value)
{
    if (d.contains(key))
    {
        object o = d[key];
        string s = o.cast<string>();
        const char *buf = s.c_str();
        strcpy(value, buf);
    }
};
```

调用方式：`getString(req, "BrokerID", myreq.BrokerID);`

> **注意 `getString` 与其他三个函数的调用差异**：`getString` 直接传递数组名（因为模板接受数组引用），而 `getInt`、`getDouble`、`getChar` 传递指针（`&myreq.XXX`）。

### 2.5 编码转换 toUtf()

```cpp
inline string toUtf(const string &gb2312)
{
    const static locale loc("zh_CN.GB18030");

    vector<wchar_t> wstr(gb2312.size());
    wchar_t* wstrEnd = nullptr;
    const char* gbEnd = nullptr;
    mbstate_t state = {};
    int res = use_facet<codecvt<wchar_t, char, mbstate_t>>
        (loc).in(state,
            gb2312.data(), gb2312.data() + gb2312.size(), gbEnd,
            wstr.data(), wstr.data() + wstr.size(), wstrEnd);

    if (codecvt_base::ok == res)
    {
        wstring_convert<codecvt_utf8<wchar_t>> cutf8;
        return cutf8.to_bytes(wstring(wstr.data(), wstrEnd));
    }

    return string();
}
```

**用途**：CTP API 所有字符串字段使用 GBK 编码。`toUtf()` 将 GBK 字符串转为 UTF-8，在**回调处理阶段**（struct→dict 转换时）对所有 `char[]` 字段调用。

**使用场景**：仅用于回调方向（C++ → Python），请求方向（Python → C++）不需要转换，因为 Python 传入的字符串已经是 UTF-8，CTP 服务器能接受。

---

## 3. CTP 数据类型映射规则

CTP SDK 的字段类型定义在 `ThostFtdcUserApiDataType.h` 中，全部通过 `typedef` 声明。共有四种基本类型模式：

### 3.1 字符串数组 — `typedef char XXXType[N]`

```cpp
// 示例
typedef char TThostFtdcBrokerIDType[11];
typedef char TThostFtdcInstrumentIDType[81];
typedef char TThostFtdcDateType[9];
```

**映射规则**：
- 回调方向（struct→dict）：使用 `toUtf()` 转为 UTF-8 字符串
  ```cpp
  data["BrokerID"] = toUtf(task_data->BrokerID);
  ```
- 请求方向（dict→struct）：使用 `getString()` 复制
  ```cpp
  getString(req, "BrokerID", myreq.BrokerID);
  ```
- Python 端类型：`str`

### 3.2 单字符 — `typedef char XXXType`（无数组维度）

```cpp
// 示例
typedef char TThostFtdcExchangePropertyType;    // 交易所属性
typedef char TThostFtdcDirectionType;           // 买卖方向
typedef char TThostFtdcOrderPriceTypeType;      // 报单价格条件
```

**识别方法**：在 `ThostFtdcUserApiDataType.h` 中，这类 typedef 通常紧跟着一组 `#define` 常量（如 `'0'`、`'1'`），表示枚举值。

**映射规则**：
- 回调方向：直接赋值（pybind11 自动将 `char` 转为长度 1 的 Python `str`）
  ```cpp
  data["Direction"] = task_data->Direction;
  ```
- 请求方向：使用 `getChar()`
  ```cpp
  getChar(req, "Direction", &myreq.Direction);
  ```
- Python 端类型：`str`（长度为 1）

### 3.3 整数 — `typedef int XXXType`

```cpp
// 示例
typedef int TThostFtdcVolumeType;
typedef int TThostFtdcFrontIDType;
typedef int TThostFtdcSessionIDType;
typedef int TThostFtdcErrorIDType;
```

**映射规则**：
- 回调方向：直接赋值
  ```cpp
  data["Volume"] = task_data->Volume;
  ```
- 请求方向：使用 `getInt()`
  ```cpp
  getInt(req, "VolumeTotalOriginal", &myreq.VolumeTotalOriginal);
  ```
- Python 端类型：`int`

### 3.4 浮点数 — `typedef double XXXType`

```cpp
// 示例
typedef double TThostFtdcPriceType;
typedef double TThostFtdcMoneyType;
typedef double TThostFtdcRatioType;
```

**映射规则**：
- 回调方向：直接赋值
  ```cpp
  data["LimitPrice"] = task_data->LimitPrice;
  ```
- 请求方向：使用 `getDouble()`
  ```cpp
  getDouble(req, "LimitPrice", &myreq.LimitPrice);
  ```
- Python 端类型：`float`

### 3.5 判断字段类型的方法

1. 打开 `ThostFtdcUserApiStruct.h`，找到目标结构体（如 `CThostFtdcInputOrderField`）
2. 查看字段的类型名（如 `TThostFtdcPriceType LimitPrice`）
3. 在 `ThostFtdcUserApiDataType.h` 中搜索该类型名
4. 根据 typedef 的目标类型判断：
   - `char XXX[N]` → getString / toUtf
   - `char XXX`（无维度） → getChar / 直接赋值
   - `int` → getInt / 直接赋值
   - `double` → getDouble / 直接赋值

---

## 4. 包装类的类结构

以 MdApi 为模板详细说明，TdApi 结构完全一致。

### 4.1 继承关系

```cpp
class MdApi : public CThostFtdcMdSpi
```

包装类继承 CTP 官方的 Spi（回调接口）基类。CTP API 通过 `RegisterSpi(this)` 将包装类注册为回调接收者。

### 4.2 私有成员

```cpp
private:
    CThostFtdcMdApi* api;           // CTP 官方 API 对象指针
    thread task_thread;              // 工作线程（从队列取任务、转换并推送到 Python）
    TaskQueue task_queue;            // 线程安全任务队列
    bool active = false;             // 工作状态标志
```

### 4.3 构造与析构

```cpp
public:
    MdApi() {};

    virtual ~MdApi()
    {
        if (this->active)
        {
            this->exit();
        }
    };
```

析构函数确保在对象销毁时安全退出（停止工作线程、释放 API）。

### 4.4 四层方法设计

包装类的方法严格分为四层，每层职责清晰：

#### 第 1 层：CTP 回调重写方法（`On*` 大写开头）

```cpp
virtual void OnFrontConnected();
virtual void OnRspUserLogin(CThostFtdcRspUserLoginField*, CThostFtdcRspInfoField*, int, bool);
virtual void OnRtnDepthMarketData(CThostFtdcDepthMarketDataField*);
```

- **调用者**：CTP 内部线程
- **职责**：深拷贝 CTP 数据 → 构造 Task → 推入队列
- **不持有 GIL**，不操作 Python 对象

#### 第 2 层：任务处理方法（`process*`）

```cpp
void processTask();                          // 主循环
void processFrontConnected(Task *task);
void processRspUserLogin(Task *task);
void processRtnDepthMarketData(Task *task);
```

- **调用者**：工作线程
- **职责**：`processTask()` 从队列取 Task 并通过 switch 分发到具体的 `process*` 方法
- 具体 `process*` 方法：获取 GIL → struct→dict 转换 → 调用第 3 层回调

#### 第 3 层：Python 虚回调方法（`on*` 小写开头）

```cpp
virtual void onFrontConnected() {};
virtual void onRspUserLogin(const dict &data, const dict &error, int reqid, bool last) {};
virtual void onRtnDepthMarketData(const dict &data) {};
```

- **默认实现**：空函数体
- **职责**：提供给 Python 子类重写的接口
- 参数已经是 Python 友好类型（`dict`、`int`、`bool`）

#### 第 4 层：请求方法（`req*` / `subscribe*` 等）

```cpp
void createFtdcMdApi(string pszFlowPath = "", bool bIsProductionMode = true);
void registerFront(string pszFrontAddress);
int subscribeMarketData(string instrumentID);
int reqUserLogin(const dict &req, int reqid);
```

- **调用者**：Python 主动调用
- **职责**：将 Python 参数转换为 CTP 结构体，调用官方 API 方法

### 4.5 生命周期方法

```
createFtdcMdApi(flowPath)     创建 API 实例 + 注册 Spi
    ↓
registerFront(address)         注册前置服务器地址
    ↓
init()                         启动工作线程 + 初始化 API 连接
    ↓
... 正常业务（登录、订阅、接收行情）...
    ↓
exit()                         停止工作线程 + 释放 API
```

`init()` 实现：

```cpp
void MdApi::init()
{
    this->active = true;
    this->task_thread = thread(&MdApi::processTask, this);  // 启动工作线程
    this->api->Init();                                       // 启动 CTP 连接
};
```

`exit()` 实现：

```cpp
int MdApi::exit()
{
    this->active = false;
    this->task_queue.terminate();    // 终止队列，唤醒工作线程
    this->task_thread.join();        // 等待工作线程退出

    this->api->RegisterSpi(NULL);    // 取消回调注册
    this->api->Release();            // 释放 CTP API
    this->api = NULL;
    return 1;
};
```

### 4.6 头文件中的常量定义

每个回调函数对应一个 `#define` 常量，用于 Task 分发：

```cpp
#define ONFRONTCONNECTED 0
#define ONFRONTDISCONNECTED 1
#define ONHEARTBEATWARNING 2
#define ONRSPUSERLOGIN 3
// ... 从 0 开始递增
```

---

## 5. 回调处理的三阶段流水线

数据从 CTP 到 Python 经过三个阶段，每种回调签名对应一种固定模式。

### 5.1 模式 A — OnRsp*（请求响应）

最常见的模式，用于所有请求-响应型回调。

**CTP 原始签名**：
```cpp
void OnRspXxx(CThostFtdcXxxField *pData, CThostFtdcRspInfoField *pRspInfo, int nRequestID, bool bIsLast)
```

**Python 端签名**：
```python
def onRspXxx(self, data: dict, error: dict, reqid: int, last: bool) -> None
```

#### 阶段 1：CTP 回调 → 深拷贝 + 入队

```cpp
void MdApi::OnRspUserLogin(CThostFtdcRspUserLoginField *pRspUserLogin,
                           CThostFtdcRspInfoField *pRspInfo,
                           int nRequestID, bool bIsLast)
{
    Task task = Task();
    task.task_name = ONRSPUSERLOGIN;

    // 深拷贝数据（CTP 指针可能为 NULL）
    if (pRspUserLogin)
    {
        CThostFtdcRspUserLoginField *task_data = new CThostFtdcRspUserLoginField();
        *task_data = *pRspUserLogin;
        task.task_data = task_data;
    }

    // 深拷贝错误信息（CTP 指针可能为 NULL）
    if (pRspInfo)
    {
        CThostFtdcRspInfoField *task_error = new CThostFtdcRspInfoField();
        *task_error = *pRspInfo;
        task.task_error = task_error;
    }

    task.task_id = nRequestID;
    task.task_last = bIsLast;
    this->task_queue.push(task);
};
```

**关键点**：
- 必须深拷贝（`new` + 解引用赋值），因为 CTP 传入的指针在回调返回后即失效
- 必须判空（`if (pXxx)`），CTP 可能传 NULL 指针
- 不获取 GIL，不操作 Python 对象

#### 阶段 2：processTask switch 分发

```cpp
void MdApi::processTask()
{
    try
    {
        while (this->active)
        {
            Task task = this->task_queue.pop();  // 阻塞等待

            switch (task.task_name)
            {
            case ONFRONTCONNECTED:
            {
                this->processFrontConnected(&task);
                break;
            }
            case ONRSPUSERLOGIN:
            {
                this->processRspUserLogin(&task);
                break;
            }
            // ... 每个回调一个 case
            };
        }
    }
    catch (const TerminatedError&)
    {
        // terminate() 被调用，安静退出
    }
};
```

#### 阶段 3：process* → GIL 获取 → struct→dict 转换 → 调用 Python 回调

```cpp
void MdApi::processRspUserLogin(Task *task)
{
    gil_scoped_acquire acquire;    // 获取 GIL

    // 转换数据字段
    dict data;
    if (task->task_data)
    {
        CThostFtdcRspUserLoginField *task_data =
            (CThostFtdcRspUserLoginField*)task->task_data;
        data["TradingDay"] = toUtf(task_data->TradingDay);   // char[] → toUtf
        data["FrontID"] = task_data->FrontID;                 // int → 直接赋值
        data["SessionID"] = task_data->SessionID;             // int → 直接赋值
        // ... 逐字段转换
        delete task_data;    // 释放深拷贝的内存
    }

    // 转换错误信息（固定模式，所有 Rsp 回调通用）
    dict error;
    if (task->task_error)
    {
        CThostFtdcRspInfoField *task_error =
            (CThostFtdcRspInfoField*)task->task_error;
        error["ErrorID"] = task_error->ErrorID;              // int
        error["ErrorMsg"] = toUtf(task_error->ErrorMsg);     // char[] → toUtf
        delete task_error;
    }

    // 调用 Python 虚回调
    this->onRspUserLogin(data, error, task->task_id, task->task_last);
};
```

**RspInfoField 的固定转换模式**：所有包含 `pRspInfo` 的回调，其 error dict 转换代码完全一致：

```cpp
dict error;
if (task->task_error)
{
    CThostFtdcRspInfoField *task_error = (CThostFtdcRspInfoField*)task->task_error;
    error["ErrorID"] = task_error->ErrorID;
    error["ErrorMsg"] = toUtf(task_error->ErrorMsg);
    delete task_error;
}
```

### 5.2 模式 B — OnRtn*（推送通知）

用于实时推送型回调（如行情、成交、报单状态变化）。

**CTP 原始签名**：
```cpp
void OnRtnXxx(CThostFtdcXxxField *pData)
```

**Python 端签名**：
```python
def onRtnXxx(self, data: dict) -> None
```

#### 阶段 1：CTP 回调 → 深拷贝 + 入队

```cpp
void MdApi::OnRtnDepthMarketData(CThostFtdcDepthMarketDataField *pDepthMarketData)
{
    Task task = Task();
    task.task_name = ONRTNDEPTHMARKETDATA;
    if (pDepthMarketData)
    {
        CThostFtdcDepthMarketDataField *task_data = new CThostFtdcDepthMarketDataField();
        *task_data = *pDepthMarketData;
        task.task_data = task_data;
    }
    this->task_queue.push(task);
};
```

**与模式 A 的差异**：无 `pRspInfo`、无 `nRequestID`、无 `bIsLast`，只有数据指针。

#### 阶段 2：同模式 A，switch 分发

#### 阶段 3：process* → 仅转换 data，调用时仅传 data

```cpp
void MdApi::processRtnDepthMarketData(Task *task)
{
    gil_scoped_acquire acquire;
    dict data;
    if (task->task_data)
    {
        CThostFtdcDepthMarketDataField *task_data =
            (CThostFtdcDepthMarketDataField*)task->task_data;
        data["TradingDay"] = toUtf(task_data->TradingDay);
        data["LastPrice"] = task_data->LastPrice;
        data["Volume"] = task_data->Volume;
        // ... 逐字段转换
        delete task_data;
    }
    this->onRtnDepthMarketData(data);    // 仅传 data
};
```

### 5.3 模式 C — OnErrRtn*（错误推送）

用于操作失败的实时推送（如报单录入失败、撤单失败）。

**CTP 原始签名**：
```cpp
void OnErrRtnXxx(CThostFtdcXxxField *pData, CThostFtdcRspInfoField *pRspInfo)
```

**Python 端签名**：
```python
def onErrRtnXxx(self, data: dict, error: dict) -> None
```

#### 阶段 1：CTP 回调 → 深拷贝 data + error + 入队

```cpp
void TdApi::OnErrRtnOrderInsert(CThostFtdcInputOrderField *pInputOrder,
                                CThostFtdcRspInfoField *pRspInfo)
{
    Task task = Task();
    task.task_name = ONERRRTNORDERINSERT;
    if (pInputOrder)
    {
        CThostFtdcInputOrderField *task_data = new CThostFtdcInputOrderField();
        *task_data = *pInputOrder;
        task.task_data = task_data;
    }
    if (pRspInfo)
    {
        CThostFtdcRspInfoField *task_error = new CThostFtdcRspInfoField();
        *task_error = *pRspInfo;
        task.task_error = task_error;
    }
    this->task_queue.push(task);
};
```

**与模式 A 的差异**：有 data 和 error，但无 `nRequestID` 和 `bIsLast`。

#### 阶段 2：同上，switch 分发

#### 阶段 3：process* → 转换 data + error，调用时传 data 和 error

```cpp
void TdApi::processErrRtnOrderInsert(Task *task)
{
    gil_scoped_acquire acquire;
    dict data;
    if (task->task_data)
    {
        CThostFtdcInputOrderField *task_data =
            (CThostFtdcInputOrderField*)task->task_data;
        data["BrokerID"] = toUtf(task_data->BrokerID);
        data["Direction"] = task_data->Direction;           // 单字符直接赋值
        data["LimitPrice"] = task_data->LimitPrice;         // double 直接赋值
        data["VolumeTotalOriginal"] = task_data->VolumeTotalOriginal;  // int 直接赋值
        // ... 逐字段转换
        delete task_data;
    }
    dict error;
    if (task->task_error)
    {
        CThostFtdcRspInfoField *task_error =
            (CThostFtdcRspInfoField*)task->task_error;
        error["ErrorID"] = task_error->ErrorID;
        error["ErrorMsg"] = toUtf(task_error->ErrorMsg);
        delete task_error;
    }
    this->onErrRtnOrderInsert(data, error);    // 传 data + error，无 reqid/last
};
```

### 5.4 特殊回调

少数回调不属于上述三种标准模式：

| 回调 | 特殊之处 |
|------|----------|
| `OnFrontConnected` | 无参数，Task 仅设 `task_name` |
| `OnFrontDisconnected(int nReason)` | 整型参数存入 `task.task_id` |
| `OnHeartBeatWarning(int nTimeLapse)` | 整型参数存入 `task.task_id` |
| `OnRspError` | 无 data 指针，仅有 error + reqid + last |

---

## 6. 请求方法的代码模式

### 6.1 模式 A — dict 参数请求

绝大多数请求方法使用此模式，接受一个 Python `dict` 和请求 ID。

**签名**：
```cpp
int reqXxx(const dict &req, int reqid)
```

**完整模板**：

```cpp
int MdApi::reqUserLogin(const dict &req, int reqid)
{
    // 1. 创建 CTP 结构体并清零
    CThostFtdcReqUserLoginField myreq = CThostFtdcReqUserLoginField();
    memset(&myreq, 0, sizeof(myreq));

    // 2. 从 dict 提取各字段（根据字段类型选择对应函数）
    getString(req, "TradingDay", myreq.TradingDay);           // char[] 字段
    getString(req, "BrokerID", myreq.BrokerID);
    getString(req, "UserID", myreq.UserID);
    getString(req, "Password", myreq.Password);
    getInt(req, "ClientIPPort", &myreq.ClientIPPort);          // int 字段
    getString(req, "ClientIPAddress", myreq.ClientIPAddress);

    // 3. 调用 CTP 官方 API
    int i = this->api->ReqUserLogin(&myreq, reqid);
    return i;
};
```

**关键点**：
- `memset(&myreq, 0, sizeof(myreq))` 确保所有未设置的字段为零值
- 字段提取函数的选择见[第 3 节](#3-ctp-数据类型映射规则)
- 返回值为 CTP API 的返回码（0 表示成功）

### 6.2 模式 B — 简单参数请求

用于订阅/取消订阅类方法，参数为单个字符串。

**签名**：
```cpp
int subscribeXxx(string instrumentID)
```

**完整模板**：

```cpp
int MdApi::subscribeMarketData(string instrumentID)
{
    char* buffer = (char*)instrumentID.c_str();
    char* myreq[1] = { buffer };
    int i = this->api->SubscribeMarketData(myreq, 1);
    return i;
};
```

**说明**：CTP 的订阅接口接受 `char**` 数组和数量，这里固定传 1 个合约。

### 6.3 无参数/特殊参数请求

部分方法的参数模式较为特殊：

```cpp
// dict 参数但无 reqid（注册类方法）
void MdApi::registerFensUserInfo(const dict &req)
{
    CThostFtdcFensUserInfoField myreq = CThostFtdcFensUserInfoField();
    memset(&myreq, 0, sizeof(myreq));
    getString(req, "BrokerID", myreq.BrokerID);
    getString(req, "UserID", myreq.UserID);
    getChar(req, "LoginMode", &myreq.LoginMode);
    this->api->RegisterFensUserInfo(&myreq);
};

// 字符串参数（前置地址注册）
void MdApi::registerFront(string pszFrontAddress)
{
    this->api->RegisterFront((char*)pszFrontAddress.c_str());
};
```

---

## 7. PyXxxApi 的 pybind11 trampoline 类

pybind11 要求 Python 子类能重写 C++ 虚函数时，需要一个 trampoline 类。该类位于 `.cpp` 文件中。

### 7.1 标准模板

```cpp
class PyMdApi : public MdApi
{
public:
    using MdApi::MdApi;    // 继承构造函数

    // 为每个 on* 虚回调方法提供 trampoline
    void onFrontConnected() override
    {
        try
        {
            PYBIND11_OVERLOAD(void, MdApi, onFrontConnected);
        }
        catch (const error_already_set &e)
        {
            cout << e.what() << endl;
        }
    };

    void onRspUserLogin(const dict &data, const dict &error, int reqid, bool last) override
    {
        try
        {
            PYBIND11_OVERLOAD(void, MdApi, onRspUserLogin, data, error, reqid, last);
        }
        catch (const error_already_set &e)
        {
            cout << e.what() << endl;
        }
    };

    void onRtnDepthMarketData(const dict &data) override
    {
        try
        {
            PYBIND11_OVERLOAD(void, MdApi, onRtnDepthMarketData, data);
        }
        catch (const error_already_set &e)
        {
            cout << e.what() << endl;
        }
    };

    // ... 每个 on* 方法都遵循相同模式
};
```

### 7.2 PYBIND11_OVERLOAD 宏

```cpp
PYBIND11_OVERLOAD(返回类型, 基类名, 方法名, 参数...);
```

该宏的作用：
1. 检查 Python 子类是否重写了该方法
2. 如果重写了，调用 Python 实现
3. 如果未重写，调用 C++ 基类的默认实现（空函数体）

### 7.3 异常捕获

每个 trampoline 方法都包裹在 `try-catch` 中，捕获 `pybind11::error_already_set`。这是因为 Python 回调中抛出的异常会被 pybind11 转换为该 C++ 异常。捕获后打印错误信息，避免异常导致工作线程崩溃。

---

## 8. PYBIND11_MODULE 注册

### 8.1 模块声明

```cpp
PYBIND11_MODULE(vnctpmd, m)
{
    class_<MdApi, PyMdApi> mdapi(m, "MdApi", module_local());
    mdapi
        .def(init<>())
        // ... 方法注册
        ;
}
```

**关键要素**：
- `PYBIND11_MODULE(vnctpmd, m)`：模块名必须与 `setup.py` 中的扩展名一致
- `class_<MdApi, PyMdApi>`：第一个类型参数是实际类，第二个是 trampoline 类
- `module_local()`：使该类型的绑定对模块局部可见，避免多个扩展模块间的类型冲突

### 8.2 方法注册顺序

注册分为三组，按以下顺序排列：

**第一组：生命周期和工具方法**

```cpp
.def(init<>())
.def("createFtdcMdApi", &MdApi::createFtdcMdApi)
.def("release", &MdApi::release)
.def("init", &MdApi::init)
.def("join", &MdApi::join)
.def("exit", &MdApi::exit)
.def("getTradingDay", &MdApi::getTradingDay)
.def("registerFront", &MdApi::registerFront)
.def("registerNameServer", &MdApi::registerNameServer)
.def("registerFensUserInfo", &MdApi::registerFensUserInfo)
```

**第二组：请求方法**

```cpp
.def("subscribeMarketData", &MdApi::subscribeMarketData)
.def("unSubscribeMarketData", &MdApi::unSubscribeMarketData)
.def("reqUserLogin", &MdApi::reqUserLogin)
.def("reqUserLogout", &MdApi::reqUserLogout)
// ...
```

**第三组：回调方法（on* 开头）**

```cpp
.def("onFrontConnected", &MdApi::onFrontConnected)
.def("onFrontDisconnected", &MdApi::onFrontDisconnected)
.def("onRspUserLogin", &MdApi::onRspUserLogin)
.def("onRtnDepthMarketData", &MdApi::onRtnDepthMarketData)
// ...
```

> 注册回调方法使得 Python 端可以通过 `super().onXxx(...)` 调用默认实现（虽然实际上是空函数体，但注册是 pybind11 trampoline 机制正常工作所必需的）。

---

## 9. 构建系统

### 9.1 setup.py 配置

```python
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
```

### 9.2 各配置项说明

| 配置项 | 值 | 说明 |
|--------|-----|------|
| `include_dirs` | `["vnpy_ctp/api/include", "vnpy_ctp/api/vnctp"]` | CTP 头文件目录 + `vnctp.h` 所在目录 |
| `library_dirs` | `["vnpy_ctp/api"]` | CTP `.so` 文件所在目录 |
| `libraries` | `["thostmduserapi_se", "thosttraderapi_se"]` | 链接 CTP 官方动态库（lib 前缀和 .so 后缀由链接器自动补全） |
| `-std=c++17` | | C++17 标准 |
| `-O3` | | 最高优化级别 |
| `-Wno-delete-incomplete` | | 抑制删除不完整类型的警告 |
| `-Wno-sign-compare` | | 抑制有符号/无符号比较警告 |
| `runtime_library_dirs` | `["$ORIGIN"]` | 运行时在 `.so` 自身所在目录搜索依赖库，使 CTP `.so` 随 Python 包一起分发 |
| `language` | `"cpp"` | 指定语言为 C++ |

### 9.3 构建命令

```bash
# 编译 C++ 扩展（--no-build-isolation 复用 venv 中已安装的 setuptools/pybind11）
uv pip install -e . --no-build-isolation
```

---

## 10. 新增接口的 Checklist

以下步骤假设要为某个新的 CTP 类接口（如 `XxxApi`）添加 binding。

### 步骤 1：准备 SDK 文件

- [ ] 将 SDK 头文件放入 `vnpy_ctp/api/include/xxx/` 目录
- [ ] 将 SDK `.so` 动态库放入 `vnpy_ctp/api/` 目录
- [ ] 确认头文件中包含：API 接口类（`CThostFtdcXxxApi`）、Spi 回调类（`CThostFtdcXxxSpi`）、数据类型定义、数据结构定义

### 步骤 2：创建目录结构

```
vnpy_ctp/api/vnctp/vnctpxxx/
├── vnctpxxx.h
└── vnctpxxx.cpp
```

### 步骤 3：编写 .h 头文件

- [ ] `#include "vnctp.h"` 和 SDK 头文件
- [ ] 为每个回调定义 `#define ONXXX N` 常量（从 0 开始递增）
- [ ] 声明包装类 `class XxxApi : public CThostFtdcXxxSpi`
- [ ] 私有成员：`api` 指针、`task_thread`、`task_queue`、`active`
- [ ] 构造函数（空）、析构函数（调用 `exit()`）
- [ ] 按四层分组声明方法：
  1. CTP 回调重写（`virtual void OnXxx(...)` — 从 SDK Spi 头文件复制签名）
  2. 任务处理（`void processTask()` + 每个回调的 `void processXxx(Task *task)`）
  3. Python 虚回调（`virtual void onXxx(...) {}` — 参数为 dict/int/bool）
  4. 请求方法（`int reqXxx(const dict &req, int reqid)` 等）

### 步骤 4：编写 .cpp 实现文件

按以下顺序逐方法填充：

- [ ] **CTP 回调实现**：每个 `OnXxx` 方法按模式 A/B/C 编写（深拷贝 + 入队）
- [ ] **processTask 主循环**：`while(active)` + `switch(task.task_name)` 分发
- [ ] **各 processXxx 方法**：`gil_scoped_acquire` + struct→dict 转换 + `delete` + 调用 `onXxx`
- [ ] **生命周期方法**：`createFtdcXxxApi`、`init`、`exit` 等
- [ ] **请求方法**：每个 `reqXxx` 按模式 A/B 编写（`memset` + `getXxx` + 调用 API）

### 步骤 5：编写 PyXxxApi trampoline 类

- [ ] 在 .cpp 文件中声明 `class PyXxxApi : public XxxApi`
- [ ] `using XxxApi::XxxApi;`
- [ ] 为每个 `onXxx` 虚方法编写 trampoline（`PYBIND11_OVERLOAD` + `try-catch`）

### 步骤 6：编写 PYBIND11_MODULE

- [ ] `PYBIND11_MODULE(vnctpxxx, m)` — 模块名与编译产物名一致
- [ ] `class_<XxxApi, PyXxxApi>` + `module_local()`
- [ ] 按顺序注册：`init<>()` → 生命周期方法 → 请求方法 → 回调方法

### 步骤 7：修改 setup.py

- [ ] 在 `ext_modules` 列表中添加新的 `Pybind11Extension`
- [ ] 确认 `include_dirs` 包含新 SDK 头文件目录
- [ ] 确认 `libraries` 包含新 SDK 动态库名
- [ ] 如果新 SDK 动态库路径不同，可能需要调整 `library_dirs`

### 步骤 8：编译验证

```bash
uv pip install -e . --no-build-isolation
python -c "from vnpy_ctp.api.vnctpxxx import XxxApi; print('OK')"
```

---

## 附录：快速参考

### struct→dict 字段转换速查

| CTP typedef 形式 | C++ 类型 | 回调方向（struct→dict） | 请求方向（dict→struct） |
|---|---|---|---|
| `char XXXType[N]` | `char[]` | `data["Key"] = toUtf(task_data->Key)` | `getString(req, "Key", myreq.Key)` |
| `char XXXType` | `char` | `data["Key"] = task_data->Key` | `getChar(req, "Key", &myreq.Key)` |
| `int XXXType` | `int` | `data["Key"] = task_data->Key` | `getInt(req, "Key", &myreq.Key)` |
| `double XXXType` | `double` | `data["Key"] = task_data->Key` | `getDouble(req, "Key", &myreq.Key)` |

### 回调模式速查

| 模式 | CTP 签名 | Python 签名 | Task 字段使用 |
|------|----------|-------------|---------------|
| A (OnRsp*) | `(DataField*, RspInfoField*, int, bool)` | `(data, error, reqid, last)` | 全部 5 个 |
| B (OnRtn*) | `(DataField*)` | `(data,)` | `task_name` + `task_data` |
| C (OnErrRtn*) | `(DataField*, RspInfoField*)` | `(data, error)` | `task_name` + `task_data` + `task_error` |
