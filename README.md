# wjx-auto · 问卷星定时自动填写助手（demo v0.1.0）

到点自动填写并提交问卷，平时在后台静默运行，任务开始前 5 分钟用弹窗 + 提示音提醒你，留出人工干预时间。
支持 GUI 增删改任务（时间、网址、默认填写内容、启动条件、优先级），并支持"A 完成后不再填 B，否则继续填 B"这类条件与优先级玩法。

> 本工具仅用于你**自己创建或已获授权**的问卷。请勿用于伪造数据、刷票或干扰他人问卷。

---

## 1. 快速开始（离线 demo，30 秒跑通）

环境要求：Windows + Python 3.9+（本机 Python 3.15 已验证）。
默认使用内置"模拟问卷"后端，**不需要任何第三方库**；连真实问卷星只需额外安装 selenium（见 §6）。

```bat
cd wjx-auto

:: 一键自测（99 项：规则引擎 / 答案库与算术 / 题目解析 / 页面状态 / 提交判定 / 提醒 / 条件优先级 / 过期保护 / 配置读写）
python main.py selftest

:: 一键演示 A/B 场景（自动起模拟问卷服务，完全隔离，不动你的配置）
python main.py demo

:: 打开图形界面（同样会自动拉起模拟问卷服务）
python main.py gui
:: 或双击  启动GUI.bat
```

首次启动会自动生成两个示例任务（指向本地模拟问卷）：

| 任务 | 时间 | 分组 | 优先级 | 启动条件 |
| --- | --- | --- | --- | --- |
| 问卷A | 3 分钟后 | 今日问卷 | 10 | 无（到点即填） |
| 问卷B | 33 分钟后 | 今日问卷 | 5 | `not submitted('问卷A')` |

在界面上把时间改成 1~2 分钟后点「保存」，就能亲眼看到：提前 5 分钟提醒 → 到点自动读取题目、
到答案库检索答案 → 填写提交 → A 提交成功后 B 自动变为「已跳过」。

---

## 1.1 用真实问卷星跑（推荐流程）

```bat
:: 1) 安装真实浏览器后端（只需一次）
pip install --target vendor selenium

:: 2) 读取题目 + 查看答案库会填什么（不提交，最安全）
python main.py resolve --url "https://v.wjx.cn/vm/你的问卷.aspx"

:: 3) 确认无误后真实提交一次
python main.py fill --url "https://v.wjx.cn/vm/你的问卷.aspx"

:: 若提示需要安全校验，改用可见浏览器模式再试
python main.py fill --url "https://v.wjx.cn/vm/你的问卷.aspx" --headed

:: 4) 加入定时任务，之后到点自动填
python main.py add --name 我的问卷 --url "https://v.wjx.cn/vm/你的问卷.aspx" ^
                   --at "2026-10-17 09:00:00" --group 每日 --priority 10
python main.py gui
```

在界面里点「📚 答案库」可以维护"题目 → 答案"规则；点「🧪 预演(不提交)」可以先看会填什么内容。

---

## 1.2 答案库：自动读题 + 检索答案（核心能力）

实际使用中题目**顺序会变**、**会增加新题**，所以程序不按 q1/q2 的位置硬填，而是：

1. 打开问卷 → 读取每道题的**题干文本、字段名、题型、选项**（真实浏览器最准，也可纯 HTTP 解析）；
2. 用题干文本到**默认答案库**检索答案；
3. 命中即填；未命中的题目留空并记入 `data/pending_questions.json`，方便事后补答案。

匹配优先级（高 → 低）：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | 任务自定义答案 | 任务编辑里「默认填写内容（可选覆盖）」按字段名给，临时覆盖用 |
| 2 | 记住的答案 | 按**题目原文精确**命中的历史答案（`bank remember` 或 learn 时写入） |
| 3 | 答案库规则 | `text` 题目文字精确 > `text` 子串 > `regex` 正则 > `expr` 表达式 |
| 4 | 自动计算 | 题目本身就是算式（如 `11-4`、`5*8`、`3加5等于多少`）时直接算 |

答案库文件 `data/answer_bank.json`：

```json
{
  "version": 2,
  "rules": [
    {"id": "arith", "match": "expr", "pattern": "arithmetic", "answer": "",
     "note": "算术题自动计算"},
    {"id": "gender", "match": "regex", "pattern": "性别", "answer": "男"},
    {"id": "name", "match": "text", "pattern": "你的姓名", "answer": "张三"},
    {"id": "hobby", "match": "text", "pattern": "兴趣爱好", "answer": ["阅读", "运动"]}
  ],
  "remembered": {"11-4": {"answer": "7", "hits": 3}}
}
```

答案写法：单选给选项**文字**（如 `方案一`）或**序号**（`1`）；多选给列表或用逗号分隔；填空给文本。

命令行维护：

```bat
python main.py bank show                       :: 查看规则、记住的答案、未命中题目
python main.py bank add --pattern "你的学号" --answer "20260001"
python main.py bank add --match regex --pattern "性别|性别是" --answer "男"
python main.py bank remove --pattern "你的学号"
python main.py learn --url "https://v.wjx.cn/vm/xxx.aspx"   :: 交互式为未命中题目补答案
python main.py bank clear-pending              :: 清空未命中题目列表
```

界面里「📚 答案库」提供同样的增删改、启用停用、清理占位规则，以及一键把未命中题干填入新增规则。

### 删除与批量操作（答案库 / 任务管理通用）

| 操作 | 方式 |
| --- | --- |
| 多选 | `Ctrl+单击` 逐个加选、`Shift+单击` 范围选择 |
| 全选 | `Ctrl+A` 或右键菜单「全选」 |
| **删除** | **`Delete` 键**（也支持 `BackSpace`）、工具栏「删除(Del)」按钮、右键菜单「删除选中」 |
| 批量删除确认 | 弹窗逐条列出将删内容 + 依赖告警，**默认焦点在「取消」**，`回车` 与 `Esc` 都是取消 |
| 一键去重 | 答案库「一键去重」：把「匹配方式+题目+答案」完全相同的重复规则合并成一条 |

> 曾经的坑：`remove_rule` 按 `id` **或** `pattern` 删除，而答案库里同名规则很多
> （例如 18 条 `pattern=姓名`），删一条会把同名的全部删掉 —— 这就是「删除规则时删掉了其它规则」
> 的原因。现在只按 `id` 精确删除，批量删除也只处理你选中的那几条。

任务删除额外提示：任务名是否被其它任务的启动条件引用、是否与其它任务同组
（删除会改变互斥/优先级行为）、是否正在执行中。

**实测结果**（真实问卷 <https://v.wjx.cn/vm/mYv3i3D.aspx>，4 道四则运算题）：

```
题目来源: browser    共 4 题    命中 4/4
  ✔ 1. [text*] 11-4   字段 q1  答案: '7'   ← 答案库(表达式)(99%)
  ✔ 2. [text*] 5*8    字段 q2  答案: '40'  ← 答案库(表达式)(99%)
  ✔ 3. [text*] 9/3    字段 q3  答案: '3'   ← 答案库(表达式)(99%)
  ✔ 4. [text*] 8+6    字段 q4  答案: '14'  ← 答案库(表达式)(99%)
→ 提交后跳转 https://v.wjx.cn/wjx/join/completemobile2.aspx?...（"感谢您的参与"）
```

---

## 1.3 实战：定时开放 + 条件串联（真实案例）

真实跑通的场景：**原问卷 16:30 先填，若填写不成功则 16:32 继续填新问卷**。

配置（也可用 `python tools\tasks_1630_1632.py` 一键写入）：

| 任务 | 时间 | 分组 | 优先级 | 启动条件 |
| --- | --- | --- | --- | --- |
| 原问卷 | 16:30 | 今日问卷 | 10 | 无（先填它） |
| 新问卷 | 16:32 | 今日问卷 | 5 | `not submitted('原问卷')` |

判定链路（实测日志）：

```
16:25:01 ⏰ 提醒 原问卷：4 分 58 秒后开始
16:25:05 🌐 已提前打开页面：https://v.wjx.cn/vm/mYv3i3D.aspx
16:27:00 ⏰ 提醒 新问卷
16:30:03 ▶ 开始执行：原问卷
16:30:05 ✘ 原问卷：填写后端异常（浏览器启动失败）→ 未提交成功
16:31:42 ⏰ 提醒 新问卷：17 秒后开始
16:32:00 ▶ 开始执行：新问卷
16:32:15 ✔ 新问卷：提交成功（已跳转）｜识别 2 题，答案库命中 2 题
```

要点：

* **新问卷设置了定时开放**（16:32 才开放）。开放前访问会返回"很抱歉，此问卷将于 … 开放"，
  程序识别为 `not_open` 状态并**等待重试**（默认每 10 秒一次、最多等 4 分钟），不会误判成失败。
* 原问卷失败后，新问卷的条件 `not submitted('原问卷')` 成立 → 继续填写；
  若原问卷成功，新问卷会因该条件 + 同组互斥**立即被跳过**。
* 新问卷只有 2 题（`11-4`、`8+6`），字段名虽同为 `q1/q2`，但与旧问卷题目不同 ——
  因为按**题目内容**匹配，仍然答对（`7`、`14`），这就是"题目顺序/数量变化也不怕"的价值。
* **同一时间只允许一个调度器实例**（`run/app.lock` 互斥）：daemon 在跑时再开 GUI 只会
  查看/编辑，不会重复提交；第二个 daemon 会直接拒绝启动。

---

## 2. 功能对照表

| 需求 | 实现 |
| --- | --- |
| 到时间自动填写内容并提交 | 调度器按 `poll_seconds` 巡检，到点读题 → 检索答案 → 填写 → 提交（`wjx/scheduler.py`） |
| 自动获取问题并在默认答案库检索答案 | `wjx/questionnaire.py` 提取题干/字段/题型/选项；`wjx/answers.py` 按题目内容检索；顺序变化按题干匹配，新题记入 pending |
| GUI 添加/删除/更改任务（时间、网址） | 主界面工具栏 + 任务编辑对话框（`wjx/gui.py`） |
| GUI 添加/删除/更改默认填写内容/答案库 | 任务对话框「默认填写内容（可选覆盖）」+ 独立「答案库」管理窗口 |
| 依据条件启动任务 | 条件表达式引擎，函数：`status/result/attempts/submitted/enabled/name/url/group`，支持 `and or not ( ) > < ==`（`wjx/rules.py`） |
| 用户自定义优先级 | 每个任务可设「分组 + 优先级」：同组内只启动优先级最高的一个 |
| A 完成后不再填 B，否则继续填 B | ① B 的条件写 `not submitted('问卷A')`；② 同组互斥：A 提交成功后 B 立即置为「已跳过」 |
| 开始前 1~5 分钟打开网站 | 每个任务可设「提前打开页面」（默认 5 分钟，`prepare_minutes`），真实后端会在后台预加载页面 |
| 提交成功判定 | 点击提交后 URL 变化 / 出现"提交成功/感谢您的参与" → 成功 |
| 提交失败判定 | 停留原页面 + 弹窗（含"填写人数过多"）→ 失败；另区分未开放、已结束、必填校验失败、安全校验 |
| 定时开放问卷 | 识别问卷星"将于 X 开放"提示页 → `not_open`，自动等待重试直至开放 |
| 安全校验（验证码）处理 | 只**检测**不绕过：识别阿里云验证码后判定为 `captcha` 并提示人工完成；建议用 `--headed` 可见浏览器模式 |
| 后台静默运行、低内存 | 单进程 tkinter + 守护线程；可最小化到系统托盘；selenium 只在真正需要时才导入；日志自动轮转 |
| 不会重复提交 | 单实例互斥（`run/app.lock`）；同组成功后其余立即跳过；失败退避避免重试风暴 |
| 开始前 5 分钟弹窗/提示音提醒 | Windows 通知中心 toast（失败退化为置顶消息框）+ `winsound` 提示音 + 任务栏闪烁 |
| 稳定性 | 66 项内置自测覆盖规则引擎、答案库与算术、题目解析、提交判定、提醒时机、条件/优先级、过期保护、配置读写(含 BOM) |

---

## 3. 界面说明

![主界面](docs/gui-main.png)

* 工具栏：新建 / 编辑 / 删除 / 启用停用 / 立即执行 / 打开网址 / 测试填写 / 设置 / 托盘运行
* 任务列表列：状态、名称、计划时间、倒计时、分组、优先级、启动条件、上次结果（右键不适用，双击可编辑）
* 详情区：选中任务后显示网址、提醒与预打开设置、默认填写内容、最近 5 次执行记录（含失败截图路径）
* 底部日志：提醒、跳过原因、提交判定结果、异常都写在这里，同时写入 `logs/wjx.log`
* 关闭窗口：若托盘可用则转入后台静默运行（任务照常执行），托盘图标可「显示主窗口 / 立即执行 / 退出」

### 默认填写内容怎么写

通常**留空**即可（由答案库负责）。只有需要临时覆盖某道题时，才按字段名写：

```
# 键值行：值为选项序号（1 起）或选项文案；逗号分隔表示多选
q3 = 这次填自定义答案
q5 = 方案一

# 也可以直接写 JSON
{"q3": "自定义答案", "q5": ["A", "C"]}
```

字段名对应问卷星控件的 `name`（一般是 `q1`、`q2`…）。在任务对话框里点
「读取该问卷的题目」会列出每道题的题干、字段名与答案库检索结果，照着填就不会错。

---

## 4. 条件表达式

```
status('问卷A') == 'success'        # 聚合状态: idle/waiting/running/success/failed/skipped/error
result('问卷B') == 'full'           # 最近结果: success/full/validation/unknown/blocked
attempts('问卷B') < 3               # 已执行次数
submitted('问卷A')                  # 是否提交成功
not submitted('问卷A')              # A 未成功才执行
enabled('问卷A') and group('B') == '今日问卷'
```

* 参数可以写任务名称、名称片段或任务 id，不区分大小写。
* 条件为**假** → 本次跳过（不提交）；条件写错 → 保守放行并在日志里给出警告。
* 命令行校验：`python main.py check-rule "not submitted('问卷A')"`

### 优先级 / 互斥语义

* `分组` 相同的任务构成一组；同组内**只有优先级最高**的一个会被启动，其余保持「待定」。
* 一旦组内任意任务**提交成功**，其余成员立即变为「已跳过」——这就是"A 成功就不填 B"的兜底保证。
* 如果首选任务失败（例如人数已满），其余成员在满足各自条件时仍会继续执行——即"否则继续填 B"。

---

## 5. 模拟问卷服务（离线验证用）

```bat
python main.py mock                 :: 默认 http://127.0.0.1:8765
python main.py mock --full-rate 0.4 :: 让问卷B 有 40% 概率返回"填写人数过多"
python main.py mock-stop            :: 停止由自动拉起功能启动的模拟服务
```

* 浏览器直接打开 <http://127.0.0.1:8765/vm/A.aspx> 就能手动填写并看到 mock 记录到的答案。
* 强制结局（便于测试两条分支）：
  * `http://127.0.0.1:8765/vm/A.aspx?force=full` → 提交后停留原页 + 弹窗"填写人数过多"
  * `http://127.0.0.1:8765/vm/A.aspx?force=fail` → 必填校验失败，停留原页
  * `http://127.0.0.1:8765/vm/A.aspx?force=ok` → 一定成功并跳转到 `join/A_done.aspx`
* 调试接口：`/stats`（所有提交记录）、`/quota/A`（已收份数）、`POST /reset`（重置）。
* GUI / `daemon` / `run` / `demo` 若发现本地 mock 服务没在运行，会自动以独立进程拉起
  （PID 记在 `run/mock.pid`，`mock-stop` 可停止）。`demo` 使用独立端口 8799 与临时配置，
  **不会读写你的 `data/tasks.json`**。

### 后端会按网址自动路由

即使设置里是 `mock` 后端，只要任务网址不是本机模拟地址（例如 `https://v.wjx.cn/vm/xxx.aspx`），
程序会自动改用真实浏览器后端，而不是把真实问卷发给模拟服务。此时若环境没有 selenium，
任务会以「填写后端异常」结束，并在日志/结果里给出安装说明——这条路径已在自测中覆盖。

---

## 6. 连接真实问卷星

```bat
:: 一键准备：安装 selenium + 下载与本机浏览器版本匹配的驱动
python tools\setup_browser.py                 :: 默认 Edge
python tools\setup_browser.py --browser chrome

:: 或手动安装
pip install --target vendor selenium
:: 驱动下载后放到项目的 drivers\ 目录：
::   Edge   https://developer.microsoft.com/microsoft-edge/tools/webdriver/
::   Chrome https://googlechromelabs.github.io/chrome-for-testing/
```

在界面「设置」里：

1. 填写后端 = `selenium`（或 `auto`：装了 selenium 就用真实浏览器，否则回落到 mock）
2. 浏览器 = `edge` / `chrome`，浏览器驱动路径留空时会自动使用项目 `drivers\` 目录里的驱动
3. 勾选「无头模式」= 后台静默（不显示浏览器窗口）；首次调试建议先取消勾选，肉眼确认填写正确；
   若问卷要求安全校验，也用可见浏览器模式更稳
4. 「自动读取题目并在答案库中检索答案」保持勾选（默认）
5. 「点击提交后判定等待」默认 6 秒：问卷星提交后跳转通常 1~3 秒

**适配说明**（`wjx/drivers/selenium_driver.py`，均经真实问卷实测）：

| 环节 | 做法 |
| --- | --- |
| 题目容器 | 依次尝试 `div.field[data-role=fieldcontain]` → `div.field.ui-field-contain` → `div.field` → `fieldset` → `.qnr-question/.div_question` |
| 题干 | `.topichtml` → `.field-label` → `legend` → `.qtitle`，并清洗题号前缀与必填星号（保留 `5*8` 的乘号） |
| 控件定位 | **按 `name` 属性定位**（`input[name=q1]`），不再依赖位置索引——实测位置索引会串行错位 |
| 隐藏控件 | 不对 `input` 做可见性过滤；问卷星手机版控件是 `display:none`，可见的是 `a.jqradio`/`div.label`/`label`，逐个尝试并回读选中状态确认 |
| 填后校验 | 逐字段回读实际值，为空则记入"未填"列表并写进日志 |
| 拦路弹窗 | 自动点掉"您之前已经回答了部分题目，是否继续上次回答"等弹窗（优先点"继续/确定"） |
| 提交按钮 | `#submit_button` → `#ctlNext` → `.submitbtn` → `input[type=submit]` → 文本含"提交"的元素 |
| 安全校验 | 检测阿里云验证码（`#captchaWrap`/`#captchabtn`）→ 判定 `captcha` 并提示人工完成；提交返回"需要安全校验，请重新提交"时自动重试若干次 |
| 结果判定 | 跳转/出现"感谢您的参与"= 成功；停留原页 + "填写人数过多" = `full`；+ "请输入内容" = `validation` |

---

## 7. 命令行

```bat
python main.py gui                                   :: 图形界面（默认）
python main.py daemon                                :: 后台静默运行（Ctrl+C 退出）
python main.py daemon --seconds 60 --scale 60        :: 自测：加速 60 倍，跑 60 秒
python main.py list                                  :: 列出任务与状态
python main.py add --name 问卷C --url https://v.wjx.cn/vm/xxx.aspx --at "2026-01-01 12:00:00" ^
                   --group 今日问卷 --priority 8 --rule "not submitted('问卷A')"
python main.py remove --task 问卷C
python main.py run --task 问卷C                      :: 立即执行一次（自动读题+检索答案）
python main.py run --task 问卷C --manual             :: 忽略答案库，用任务里的默认内容
python main.py run --task 问卷C --dry-run            :: 只检索答案不提交

:: —— 题目 / 答案库 ——
python main.py probe   --url "https://v.wjx.cn/vm/xxx.aspx"          :: 读题+检索答案（不提交）
python main.py probe   --url "https://v.wjx.cn/vm/xxx.aspx" --static :: 不启动浏览器，HTTP 解析
python main.py resolve --url "https://v.wjx.cn/vm/xxx.aspx"          :: 预演：显示将会填写的内容
python main.py fill    --url "https://v.wjx.cn/vm/xxx.aspx"          :: 真实填写并提交
python main.py fill    --url "https://v.wjx.cn/vm/xxx.aspx" --headed :: 可见浏览器（避开安全校验）
python main.py bank show / add / remove / remember / clear-pending
python main.py learn   --url "https://v.wjx.cn/vm/xxx.aspx"          :: 交互式补答案

python main.py mock-stop                             :: 停止自动拉起的模拟服务
python main.py selftest / demo                       :: 自测 / 场景演示（demo 完全隔离，不动你的配置）
```

`--scale` 只用于自测加速时钟，正式使用请不要开启。

---

## 8. 目录结构与设计

```
wjx-auto/
├─ main.py               命令行入口（gui/mock/daemon/demo/probe/resolve/fill/bank/learn/selftest…）
├─ start_gui.py          双击启动 GUI
├─ 启动GUI.bat
├─ drivers/              放浏览器驱动（msedgedriver.exe / chromedriver.exe）
├─ wjx/
│  ├─ models.py          任务/结果/设置数据模型（含运行期状态与历史记录）
│  ├─ config.py          配置持久化（原子写 + .bak 备份）与答案文本解析
│  ├─ rules.py           条件表达式引擎（词法/语法/求值）+ 优先级互斥裁决
│  ├─ questionnaire.py   题目提取：真实 DOM 解析 + 浏览器内 JS 提取（题干/字段/题型/选项）
│  ├─ answers.py         默认答案库：算术求值、按题目内容检索、记住答案、未命中收集
│  ├─ autorun.py         流程编排：读题 → 检索答案 → 填写提交 → 记录（含 dry-run 预演）
│  ├─ scheduler.py       调度器：提前提醒 → 提前打开 → 到点执行 → 结果判定 → 条件跳过
│  ├─ drivers/
│  │  ├─ base.py         后端接口 + 提交结果判定（跳转=成功；人数过多/安全校验=失败分类）
│  │  ├─ mock.py         模拟问卷后端（HTTP，离线可跑）
│  │  └─ selenium_driver.py  真实问卷星后端（按 name 定位控件、弹窗处理、验证码检测）
│  ├─ notify.py          弹窗（toast/消息框）、提示音、任务栏闪烁
│  ├─ gui.py             tkinter 界面（任务列表/编辑/答案库管理/设置/日志/托盘）
│  ├─ mockserver.py      本地模拟问卷服务
│  ├─ manualtest.py      脚本化"跑一次"辅助
│  └─ selftest.py        内置自测
├─ data/tasks.json        你的任务配置（不入库）
├─ data/answer_bank.json  你的默认答案库（不入库）
├─ data/pending_questions.json  答案库未命中的题目（不入库）
└─ logs/wjx.log           运行日志（自动轮转）
```

填写流程（每个任务）：

```
到点 ──▶ 打开问卷 ──▶ 提取题目(题干/字段/题型/选项)
                       │
                       ├─ 答案库命中 ──▶ 填写 ──▶ 提交 ──▶ 判定
                       └─ 未命中 ──▶ 留空 + 记入 pending_questions.json
提交判定：URL 跳转/出现"感谢您的参与" = 成功
         停留原页 + "填写人数过多"     = 失败(full)
         停留原页 + "需要安全校验"     = 需人工验证(captcha)
         停留原页 + "请输入内容/必填"  = 校验失败(validation)
```

---

## 9. 已知限制 / 待你提改进意见

1. **安全校验（阿里云验证码）**：本工具**不绕过**验证码。实测同一问卷首次提交可正常通过，
   连续提交才会返回"需要安全校验，请重新提交！"并弹出验证码；程序判为 `captcha` 并提示。
   目标问卷一般只填一次，通常不会遇到；遇到时用可见浏览器模式（`--headed`）手动完成一次即可。
2. **"提前打开页面"依赖浏览器长时间存活**：实测预打开后挂着 5 分钟的浏览器会被系统回收/崩溃，
   导致任务失败。现已做两层防护：① 使用前做存活检查，失效则**自动重新启动浏览器**；
   ② 启动失败时清理残留 profile 锁并重试。**若目标问卷有定时开放，建议把「提前打开页面」关掉**
   （16:32 开放就 16:32 现开现填），更稳。
3. **登录态问卷**：需要先在有头模式登录一次并保留浏览器用户目录（目前每个浏览器后端有独立
   `run/browser-<edge|chrome>` 用户目录，可在此基础上做 profile 复用与扫码登录）。
4. **矩阵题/量表题**：已能识别（同名字段多选项判为 matrix），但答案映射还没做专门处理，
   需要时告诉我矩阵题的作答规则。
5. **多页问卷**：识别与翻页逻辑已留接口，尚未实机验证多页翻页。
6. **题目文案微调**：如果问卷方改了题干措辞，原来的 `text` 精确规则会失配；建议用 `regex`
   关键词匹配（如 `性别`），或开启"记住的答案"（题目原文精确命中，改动即失效）。
7. **验证范围**：两道真实问卷均已实机提交成功（4 题版、2 题版）；单选/多选/填空/下拉
   在模拟问卷上验证通过，真实问卷的非算术题型还需样例来实机确认。

---

## 10. 打包成可执行文件 / 安装包

推荐用**安装包**分发（setup 解包安装，不做单文件运行时解包）：

```bat
python tools\build_exe.py --setup        :: 构建 onedir + 生成安装包（推荐）
python tools\build_exe.py --setup --portable   :: 再压一个绿色版 zip
python tools\build_exe.py                :: 只构建绿色版目录
python tools\build_exe.py --only-setup   :: 只重新生成安装包（用已有 dist/wjx-auto）
python tools\build_exe.py --clean        :: 先清空 build\ 与 dist\
python tools\build_exe.py --no-driver    :: 不内置浏览器驱动（体积小约 23MB）
```

产物：

```
dist/setup/wjx-auto-<版本>-setup.exe        安装包（约 36 MB；解包后约 116 MB）
dist/setup/wjx-auto-<版本>-portable.zip     绿色版压缩包（约 57 MB，解压即用）
dist/wjx-auto/                              绿色版目录（免安装，整个拷走即可）
  wjx-auto-gui.exe                          图形界面，双击即用（无控制台窗口）
  wjx-auto.exe                              命令行版（selftest / probe / fill / autostart / daemon / bank …）
  使用说明.txt                               使用者文档
  _internal/                                Python 运行时 + 依赖（含 selenium 4.51 与 Edge 驱动）
```

> **装不上时的排查顺序**（真实案例）：
> 若安装报错里的路径含 `\AppData\Local\Temp\is-XXXX.tmp`，说明**解包用的临时目录被拦**了
> （受控文件夹访问 / 360 / 火绒 / 腾讯电脑管家等），安装程序还没开始装就被挡住，连日志都写不出。
> 此时安装包会**提前弹窗说明原因**并给出选择：生成目录诊断报告，或改用绿色版。
> 绿色版完全不需要临时目录，推荐直接解压 `*-portable.zip` 使用。
> 运行 `wjx-auto-<版本>-setup.exe /TESTDIRS` 可导出各目录可写性报告，便于定位。

### 安装包行为

> **双击即弹 UAC**：安装包**外壳**的清单已改为 `requireAdministrator`（做法见
> `tools/patch_inno_launcher.py`），Windows 在启动安装包的第一时间就会弹提权确认 ——
> 这发生在 Inno 自解包之前，因此不会再出现
> 「无法创建目录 …\Temp\is-XXXX.tmp / 错误 5: 拒绝访问」（那个错误是非管理员时
> 自解包写临时目录被拦导致的，光设 `PrivilegesRequired=admin` 解决不了，因为提权请求
> 发生在自解包之后）。**程序日常运行不需要管理员权限。**
>
> 仍然装不上时的备选：解压 `*-portable.zip` 直接用绿色版（完全不依赖临时目录），
> 或把用户环境变量 `TEMP`/`TMP` 指到可写目录（如 `D:\Temp`）后再运行安装包。

| 项目 | 说明 |
| --- | --- |
| 权限 | 安装包外壳 `requireAdministrator` + 脚本 `PrivilegesRequired=admin`：启动即弹 UAC，确认后全程管理员权限。程序本身运行时用普通权限即可 |
| 安装位置 | 管理员模式默认 `C:\Program Files\wjx-auto`（向导里可改）；`/CURRENTUSER` 装到 `%LOCALAPPDATA%\wjx-auto` |
| 运行期目录权限 | 安装时给 `data` / `logs` / `run` / `.selenium-cache` 授予「所有用户：修改」（`Permissions: users-modify`），普通用户运行也能写；程序本体仍只读 |
| 数据位置回落 | 若安装位置对当前用户不可写（例如手工拷进 Program Files），程序启动时自动改用 `%LOCALAPPDATA%\wjx-auto`，并写一份「数据位置说明.txt」 |
| 早期探针 | 向导创建后写「`桌面\wjx-auto-安装探针.txt`」（是否管理员 / TEMP 可写性 / 安装目录可写性），便于判定"提权有没有生效" |
| 自诊断 | `setup.exe /TESTDIRS` 导出各目录可写性报告，不安装任何东西 |
| 快捷方式 | 开始菜单：主程序 / 命令行 / 环境自检 / 使用说明 / 卸载；可选桌面图标与「预演本卷」 |
| 可选任务 | ① 桌面快捷方式 ② **开机自动后台运行**（HKCU Run 项，`wjx-auto-gui.exe --minimized`，启动即收进托盘） |
| 数据保留 | `data\` 标了 `uninsneveruninstall` + `onlyifdoesntexist`：升级与卸载都不覆盖/不删除 |
| 外壳补丁 | `tools/patch_inno_launcher.py`（`--check` / `--restore` / 构建时自动调用）：把 Inno 安装目录里 `SetupLdr.e32/e64` 的清单等长替换为 `requireAdministrator`（**文件大小逐字节不变**，否则 Inno 会拒绝使用）；因改过预编译文件，`.iss` 里按官方文档设 `DisablePrecompiledFileVerifications=setupldr` |
| 语言 | 简体中文（`packaging/languages/ChineseSimplified.isl`）+ 英文 |

打包要点（`packaging/` 目录）：

| 文件 | 作用 |
| --- | --- |
| `wjx-auto.spec` | 一次构建两个 exe（GUI 无控制台 + CLI 有控制台），共用同一份依赖，体积几乎不翻倍 |
| `wjx-auto.iss` | Inno Setup 脚本：按用户安装、可选开机自启、卸载保留数据 |
| `common.py` | 收集规则：只带 selenium 及其运行依赖；排除 numpy/pandas/matplotlib，省下上百 MB |
| `languages/ChineseSimplified.isl` | 安装向导的简体中文语言文件 |
| `使用说明.txt` / `空数据目录说明.txt` | 随包分发的用户文档 |

其它实现细节：

* **路径**：`wjx/paths.py` 检测 `sys.frozen`，只读资源取 `_MEIPASS`，可写数据取 exe 同级目录
  （若 exe 目录不可写，如装在 `Program Files`，自动回落到 `%LOCALAPPDATA%\wjx-auto`）。
* **HTML 解析**：用项目内置的 `wjx/htmlparse.py`（纯标准库），因此不必打包
  beautifulsoup4 + lxml（省约 30 MB）；`probe --static` 在打包版里照常可用。
* **模拟问卷服务**：打包后没有 `main.py` 可执行，改为在本进程内用线程启动
  （`mockserver.ensure_running`），GUI / demo / run 都能正常拉起。
* **驱动**：只把 `drivers/*.exe` 打进 `_internal/drivers/`（跳过 11 MB 的 zip）；
  用户也可把匹配自己浏览器版本的驱动放到安装目录的 `drivers\`。
* **自检**：`wjx-auto.exe check-env` 报告运行方式、可写目录、tkinter、内置解析器、
  selenium、浏览器与驱动、网络、答案库与任务数量 —— 换机器后先跑它就够定位问题。

已实测（打包版，非源码）：

```
wjx-auto.exe selftest   → 99 项全部通过
wjx-auto.exe check-env  → 环境就绪 ✔（selenium 4.51 + 内置 Edge 驱动）
wjx-auto.exe probe --url https://v.wjx.cn/vm/xx.aspx  → 真实无头 Edge 读出 5 道题
wjx-auto.exe demo       → 两个场景通过
从任意安装目录运行       → 正确读取该目录下 data\，任务列表正确
模拟卸载                 → 按 [UninstallDelete] 清理后 data\ 完整保留
```

安装包用 Inno Setup 编译（本机：`winget install --id JRSoftware.InnoSetup --scope user`）。
注意：当前沙箱会拒绝**安装器解包出的子进程**写 `HKCU\...\Uninstall` 注册表项，所以这里做到
"编译成功 + 解包内容与绿色版逐字节一致 + 安装后程序可用 + 卸载保留数据"；
请在正常桌面环境双击 `wjx-auto-0.5.2-setup.exe` 走一遍向导，确认快捷方式与开机自启。

---

## 11. 版本管理

仓库使用 git 规范管理，历史提交按功能拆分。
重新克隆后运行：

```bat
python main.py selftest    :: 应全部 PASS（99 项）
python main.py demo        :: 两个场景均通过
python tools\build_exe.py  :: 重新打包 exe
```
