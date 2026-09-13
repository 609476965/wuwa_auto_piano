# mc_auto_piano

21 键 Minecraft 自动弹琴脚本。传入简谱文本或 MIDI 文件，它就会按你图上的键位自动弹。

需要 Python 3.10 或更高版本（用了 `dataclass(slots=True)`）。

```
        1    2    3    4    5    6    7
高音    Q    W    E    R    T    Y    U
中音    A    S    D    F    G    H    J
低音    Z    X    C    V    B    N    M
```

## 特点

- **零依赖运行**：简谱解析 + MIDI 解析 + 按键发送全部内置，不装任何第三方库也能跑。
- **两种曲谱输入**：自写简谱文本，或直接丢 `.mid` 文件进去。
- **精确调度**：用 `perf_counter` + Windows 1ms 计时器精度排程，长曲子也不会积累漂移。
- **游戏兼容**：默认走 `pydirectinput`，没装时自动回退到内置的 `SendInput`（两者底层都是扫描码注入）。
- **随时中断**：播放中按 **F8** 立即停止。
- **安全预演**：`--dry-run` 只打印时间轴，一个键都不按。

## 快速开始

```powershell
# 看一眼键位表和记谱法
python mc_piano.py --list-keys

# 先预演，确认解析结果对不对
python mc_piano.py scores\小星星.txt --dry-run

# 正式弹奏（有 3 秒倒计时，期间切到游戏窗口）
python mc_piano.py scores\小星星.txt

# 推荐：指定游戏窗口，脚本会自己切焦点；权限不够会自己请求提权
python mc_piano.py scores\小星星.txt --window "鸣潮"

# 弹 MIDI
python mc_piano.py 某首曲子.mid --track 0 --transpose -12

# 直接内联一小段
python mc_piano.py "1 2 3 1 | 3 2 1 -" --bpm 100
```

跑起来后终端会有倒计时，**先切到游戏窗口并保持焦点**，倒计时结束就开始按键。想停就按 `F8`。

> 游戏以管理员身份运行时（鸣潮等带反作弊的游戏基本都是），普通权限的脚本发的按键
> 会被 Windows 静默丢弃。用 `--window "标题"` 让脚本自动发现并请求提权，或者直接
> `--elevate`。详见下面的[排查章节](#游戏没反应先跑自检)。

## 记谱法

写在自己的 `.txt` 里，或者直接当命令行参数传进来。UTF-8 / GBK 都能读。

| 写法 | 含义 |
| --- | --- |
| `1` … `7` | 音级 do…si（默认中音区） |
| `^1` / `_1` | 高音 / 低音；也支持 `h1` `m1` `l1` |
| `1:2` | 时值 2 拍；支持小数 `1:0.5` 和分数 `1:1/2` |
| `-` | 延音线，把前一个音再延长一拍 |
| `0` | 休止符 |
| `1+3+5` | 和弦，几个键同时按下 |
| `\|` | 小节线，纯装饰，会被忽略 |
| `//` `;` `#` | 注释（`#` 需在行首） |

行首指令（作用于之后的内容）：

```
@title=小星星      # 曲名，只是显示用
@bpm=100           # 每分钟拍数
@beat=0.25         # 每拍秒数，直接指定（会覆盖 @bpm）
@gate=0.85         # 按键时长占音符时值的比例
@octave=mid        # 后面没写八度标记的音符默认落在哪个八度
```

`1 + 3 + 5`、`1 : 2` 这种带空格的写法也能识别。

关于半音：这台琴只有 7 个白键，`4#`、`5b` 这类会就近吸附到自然音级并给出警告，`--snap up` 可以改成向上吸附。

完整例子见 `scores\记谱法示例.txt`。

## MIDI 支持

内置了一个标准 MIDI 文件（SMF）解析器，支持 format 0/1/2、running status、多轨合并、变速（tempo map），**不需要装 mido**。

```powershell
python mc_piano.py song.mid                      # 合并所有轨道
python mc_piano.py song.mid --track 1            # 只弹第 2 条轨道
python mc_piano.py song.mid --quantize 0.02      # 起音对齐到 20ms 网格，去掉抖动
python mc_piano.py song.mid --transpose -12      # 整体降一个八度
python mc_piano.py song.mid --midi-engine mido   # 内置解析器啃不动时改用 mido
```

本琴只能覆盖 3 个八度（默认 低音 C3 ～ 高音 B5，即 MIDI 48–83）。超范围的音会被整体移调对齐并提示，用 `--transpose` 可以自己调。

## 参数速查

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--bpm` | `120` | 每分钟拍数（简谱用） |
| `--speed` | `1.0` | 整体速度倍率，`1.2` 表示快 20% |
| `--gate` | `0.9` | 按下时长占音符时值的比例 |
| `--min-hold` | `0.035` | 单个键最短按住时间（秒），防止太短被游戏丢掉 |
| `--min-gap` | `0.012` | 同一个键两次按下之间的最小间隔（秒） |
| `--base` | `60` | 「中音 1」对应的 MIDI 音高 |
| `--transpose` | `0` | 移调半音数 |
| `--snap` | `down` | 半音吸附方向，`down` / `up` |
| `--octave` | `mid` | 简谱默认八度 |
| `--track` | 全部 | MIDI：只弹指定轨道（从 0 开始） |
| `--quantize` | `0` | MIDI：起音对齐网格（秒） |
| `--backend` | `auto` | `auto` / `pydirectinput` / `sendinput` / `keybd_event` / `postmessage` / `keyboard` / `pyautogui` |
| `--window` | — | 目标窗口标题的一段字，指定后自动切焦点 |
| `--elevate` | — | 以管理员身份重新启动自己 |
| `--no-elevate` | — | 检测到需要提权时不自动提权，只提示 |
| `--countdown` | `3` | 开始前倒计时秒数 |
| `--loop` | `1` | 循环遍数 |
| `--test` | — | 弹一段爬音阶，用来确认游戏收不收按键 |
| `--diagnose` | — | 环境自检 |
| `--list-windows` | — | 列出所有可见窗口 |
| `--dry-run` | — | 只解析打印，不按键 |
| `--verbose` | — | 实时打印每一次按下/抬起 |

## 游戏没反应？先跑自检

```powershell
python mc_piano.py --diagnose      # 权限 / 注入 / 焦点逐条查
python mc_piano.py --list-windows  # 拿游戏窗口的准确标题
python mc_piano.py --test          # 弹一段爬音阶，听游戏有没有声音
```

### 头号原因：权限不对等（UIPI）

带反作弊的游戏（**鸣潮**的 ACE、部分带 EAC/BE 的游戏）会**强制以管理员身份运行**。
Windows 有一条硬规则：**普通权限的进程发往管理员权限窗口的按键会被静默丢弃**，
既不报错也没有任何提示，表现就是「脚本明明在跑，游戏一点反应都没有」。

自检会直接点出来：

```
  --- 权限检查 ---
  [管理员]     Client-Win64-Shipping.exe  鸣潮
  >>> 找到问题了 <<<
```

三种解决办法，任选其一：

```powershell
# 1. 让脚本自己弹 UAC 提权（最省事）
python mc_piano.py scores\小星星.txt --window "鸣潮" --elevate

# 2. 指定 --window 后直接跑，脚本发现权限不足会自动请求提权
python mc_piano.py scores\小星星.txt --window "鸣潮"

# 3. 右键终端 / PowerShell → 以管理员身份运行，然后照常执行
```

正式开弹前脚本还会**再检查一次前台窗口的权限**，不对就立刻停下来说清楚，
不会让你白等一首曲子的时间。加 `--no-elevate` 可以关掉自动提权、只提示。

### 其他原因

| 现象 | 处理 |
| --- | --- |
| 窗口没焦点 | 加 `--window "游戏标题"`，脚本会自动把游戏切到前台（倒计时前后各切一次） |
| 游戏只认原始输入，注入的按键不认 | `--backend postmessage --window "游戏标题"`，直接往窗口投递按键消息 |
| 某个后端不灵 | 依次试 `--backend pydirectinput` / `keybd_event` / `sendinput` / `keyboard` |
| 输入法吞键 | 切到英文输入法 |
| 按键太短被游戏丢掉 | 调大 `--min-hold`（如 `0.06`）或调小 `--gate`（如 `0.7`） |
| 游戏里改过按键绑定 | 确认 Q W E R T Y U / A S D F G H J / Z X C V B N M 没被占用 |

**节奏整体偏快或偏慢**：用 `--speed 0.9` 微调，或者简谱里改 `@bpm`。

**想中途停**：按 `F8`。

## MIDI 转谱：`midi2score.py`

21 键琴是「3 个八度 + 只有白键」，真实 MIDI 几乎不可能直接弹。这个工具负责把 MIDI
改造成弹得出来的谱子，并告诉你它改了什么。

```powershell
python midi2score.py song.mid                    # 转换，谱子打到标准输出
python midi2score.py song.mid --report-only      # 只看分析报告
python midi2score.py song.mid -o song.txt        # 写到文件
python midi2score.py song.mid --melody           # 只保留主旋律

# 一条龙：转换 + 直接弹
python midi2score.py song.mid -o s.txt --play --window "鸣潮"
```

报告走 stderr、谱子走 stdout，所以 `python midi2score.py song.mid > out.txt` 拿到的是干净的谱子。

### 它解决了什么

| 问题 | 处理方式 |
| --- | --- |
| **音域超了**（琴只有 MIDI 48–83） | 按八度折叠回音域内，并尽量选折叠次数最少的方案 |
| **有黑键**（琴只有 C 大调白键） | 先搜索最佳移调把整首搬到白键上，残余的再吸附到相邻白键 |
| **和弦太厚**（只有 21 个键） | 默认每个起音最多留 3 个音，保旋律和低音 |
| **有鼓轨** | 第 10 通道直接丢掉 |
| **节奏量化** | 自动挑「最粗但不被听出来」的网格（判据是 25ms 绝对误差） |

### 关键设计：先移调，再吸附

黑键不该靠硬吸附解决。比如 D 大调的歌有两三个黑键，直接吸附会把旋律改得很难听；
但整首降 2 个半音就变成纯 C 大调，**一个黑键都不剩**。所以工具会先把 12 个移调量
× 7 个八度偏移全试一遍，按「落在白键上的时长 − 吸附惩罚 − 折叠惩罚」打分选最优。

### 报告会告诉你改了什么

```
=== 转换报告 ===
  使用轨     : 第 1 轨（2 条轨道里只有这条有音符）
  量化网格   : 1/32（平均误差 12.2 tick）
  原曲调性   : B 大调
  原曲音域   : MIDI 27–100（6.1 个八度）

  移调       : +1 半音
  转换后调性 : C 大调
  转换后音域 : MIDI 48–83（2.9 个八度）

  输出音符   : 2305 个
  八度折叠   : 575 个（超出 3 个八度，挪回音域内）
  半音吸附   : 100 个（黑键吸附到相邻白键）
  减声部     : ...
  长音缩短   : ...
  主导时值   : 0.6 拍（占 18% 的音符）
               常见时值不是整数拍，这个文件的速度标记很可能是默认填的。
```

### 两个容易踩的坑

**1. 简谱是顺序语义，长音会被提前收。**
简谱里每个音从前一个音的结束处开始，所以撑不到下一个音的长音必须缩短（报告里的「长音缩短」）。
默认 `--hold gap` 保证**时间轴和原曲严格对齐**；`--hold true` 保留真实时值，
但只要有音符重叠，后面所有音就会整体后移。单声部素材两者结果完全一样。

**2. 不少 MIDI 的速度标记是默认填的。**
报告里的「主导时值」如果明显不是整数拍（比如 0.6 拍），说明文件的速度八成不对。
用 `--bpm 96` 按耳朵校正即可（相对的速度变化会保留）。

### 常用参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--melody` | — | 只保留主旋律（自动选轨 + 每个起音只留最高音） |
| `--max-notes` | `3` | 同一时刻最多几个音，`0` = 不限 |
| `--reduce` | `spread` | 减声部时留哪些：`spread` 保旋律和低音 / `high` / `low` |
| `--grid` | `auto` | 量化网格：`auto` / `1/4` … `1/64` / `off` |
| `--transpose` | `auto` | 移调半音数，或 `auto` 自动搜索 |
| `--octave` | 自动 | 额外的八度偏移 |
| `--snap` | `smart` | 黑键吸附：`smart` 跟随旋律走向 / `down` / `up` |
| `--hold` | `gap` | 时值取法，见上面的坑 1 |
| `--bpm` | 原值 | 改写输出速度 |
| `--track` | 自动 | 只用指定的 MIDI 轨道 |
| `--min-velocity` | `1` | 丢掉太轻的音 |
| `--keep-drums` | — | 保留第 10 通道 |
| `--report-only` | — | 只分析不输出 |
| `--play` | — | 转完直接交给 `mc_piano.py` 弹 |

## 可选依赖

不装也能用。装了会更好：

```powershell
pip install -r requirements.txt
```

- `pydirectinput` —— 部分游戏用内置后端不灵时的备选，`--backend` 保持 `auto` 就会优先用它。
- `mido` —— 只在 `--midi-engine mido` 时需要，用来兜底处理奇怪格式的 MIDI。

## 目录

```
mc_piano.py              自动弹奏主脚本
midi2score.py            MIDI -> 简谱 转换器
scores\小星星.txt          入门示例
scores\生日快乐.txt        含附点时值与高音区
scores\欢乐颂.txt          含低音区与八分音符
scores\记谱法示例.txt      记谱法全功能演示
scores\haru.mid          示例 MIDI
scores\haru_melody.txt    由 haru.mid 转出的主旋律版
scores\haru_full.txt      由 haru.mid 转出的全声部版
requirements.txt         可选依赖
```

## 排查用到的 Windows API

脚本用到的都是系统自带能力，没有额外依赖：

- `SendInput` / `keybd_event` —— 注入按键；`SendInput` 的返回值会检查，被拒时报错而不是静默失败
- `GetAsyncKeyState` —— F8 中断热键
- `EnumWindows` / `GetWindowTextW` / `QueryFullProcessImageNameW` —— `--list-windows`
- `OpenProcessToken` + `GetTokenInformation(TokenElevation)` —— 判断目标窗口是不是管理员权限
- `AttachThreadInput` + `SetForegroundWindow` —— 绕过前台锁定，把游戏切到最前
- `PostMessageW` —— `postmessage` 后端，绕开焦点限制
