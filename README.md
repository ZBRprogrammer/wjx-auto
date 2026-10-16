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

:: 一键自测（60 项：规则引擎 / 答案库与算术 / 题目解析 / 提交判定 / 提醒 / 条件优先级 / 过期保护 / 配置读写）
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
| 提交失败判定 | 停留原页面 + 弹窗（含"填写人数过多"）→ 失败；另区分必填校验失败、安全校验、问卷不可用 |
| 安全校验（验证码）处理 | 只**检测**不绕过：识别阿里云验证码后判定为 `captcha` 并提示人工完成；建议用 `--headed` 可见浏览器模式 |
| 后台静默运行、低内存 | 单进程 tkinter + 守护线程；可最小化到系统托盘；selenium 只在真正需要时才导入；日志自动轮转 |
| 开始前 5 分钟弹窗/提示音提醒 | Windows 通知中心 toast（失败退化为置顶消息框）+ `winsound` 提示音 + 任务栏闪烁 |
| 稳定性 | 60 项内置自测覆盖规则引擎、答案库与算术、题目解析、提交判定、提醒时机、条件/优先级、过期保护、配置读写(含 BOM) |

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

1. **安全校验（阿里云验证码）**：本工具**不绕过**验证码。实测问卷在连续提交后会返回
   "需要安全校验，请重新提交！"并弹出阿里云验证码；程序会把结果判为 `captcha` 并给出提示。
   规避建议：用可见浏览器模式（`--headed` / 设置里关掉无头模式）、拉开任务间隔、避免高频提交。
   如果你希望"弹验证码时自动暂停并响铃提醒人工完成，完成后自动继续提交"，我可以加。
2. **登录态问卷**：需要先在有头模式登录一次并保留浏览器用户目录（目前每个浏览器后端有独立
   `run/browser-<edge|chrome>` 用户目录，可在此基础上做 profile 复用与扫码登录）。
3. **矩阵题/量表题**：已能识别（同名字段多选项判为 matrix），但答案映射还没做专门处理，
   需要时告诉我矩阵题的作答规则。
4. **多页问卷**：识别与翻页逻辑已留接口，尚未实机验证多页翻页。
5. **题目文案微调**：如果问卷方改了题干措辞，原来的 `text` 精确规则会失配；建议用 `regex`
   关键词匹配（如 `性别`），或开启"记住的答案"（题目原文精确命中，改动即失效）。
6. **验证范围**：四则运算问卷已完成真实提交验证；其他题型（单选/多选/填空/下拉）在模拟问卷
   上验证通过，真实问卷还需你提供样例问卷来实机确认。

---

## 10. 版本管理

仓库使用 git 规范管理，历史提交按功能拆分。
重新克隆后运行：

```bat
python main.py selftest    :: 应全部 PASS（60 项）
python main.py demo        :: 两个场景均通过
```
