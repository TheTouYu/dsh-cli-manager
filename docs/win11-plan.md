# Win11 迁移计划：启动移动端 UI + 运行时切换

目标（用户 2026-11-26 提出）：搬到 Windows 11 后，**一键启动原神并进入移动端 UI**，并且**运行中随时切换**移动端 / PC UI。

## 从 Linux 侧带过来的既定事实

| 事实 | 值 | 来源 |
|---|---|---|
| 输入模式字段 | `[[G+0x9EC0]+0x1AC]`，`0`=移动端 UI，`2`=PC UI | genshin-mobile 探针实测 |
| 切换函数 | `0x14D484110(nullptr)`（进程内调用，参数 NULL = 触摸模式） | 同上 |
| 运行时切换通道 | 命令文件 `<dll主名>.cmds`，探针约 1 秒轮询一次；追加 `touch_null` / `uipc` / `gain 35` | 同上 |
| 原生移动平台参数 | `use_mobile_platform -is_cloud 1 -platform_type CLOUD_THIRD_PARTY_MOBILE` | 2026-11-19 在 Wine 下试用过，未在真 Windows 复验 |
| 游戏触摸导入 | `RegisterTouchWindow` / `GetTouchInputInfo` / `CloseTouchInputHandle` 各 1 | `docs/unity-touch-notes.md` |

结论方向：**在真 Windows 上，系统触摸可能由 OS 直接投递给 Unity，探针的 WM_TOUCH 合成未必还需要；但"切成移动端 UI"这一步仍然只能进程内调用。**

## 两条路线

- **A 原生参数路线（优先试，最省事）**：用 `use_mobile_platform` 启动。若 UI 直接是移动端，就只剩"运行时切换回 PC UI"这一件事。
- **B 注入路线（Linux 已验证）**：loader 注入探针 → `touch_null` 切 UI → 之后靠 `.cmds` 运行中切换。

两条路线共用同一套 CLI 管理器命令，只是底层启动脚本不同。

## 要落的三个部件

1. **Windows launcher**：起游戏（可带原生参数）→（必要时）注入探针 → 等就绪 → 切 UI；失败要能回退到 B 路线。
2. **探针的 Windows 构建**：`probe/mobui.cpp` 本来就是 mingw 交叉编译产物；Win11 上要按游戏版本重新核对 RVA（`0x14D484110`、`kSlot9EC0` 等），RVA 表必须按版本维护。
3. **CLI 管理器跨平台**：
   - `modules/*.json` 的 `cwd` 与 `argv` 支持 `${VAR}` 环境变量展开（避免写死 `/home/...`）；
   - 每个命令增加 `platform: "linux" | "windows" | "all"`，界面只显示当前平台可用的；
   - Windows 侧命令用 `.bat` / `.exe` / `powershell -File` 包装。

## 立即可做（不依赖 Windows，先做通用增强）

- [ ] 模块 JSON：`${VAR}` 展开 + `platform` 字段过滤（同一份模块可跨平台，Win11 只多一个 `genshin-touch-win.json`）
- [ ] 命令执行器：Windows 下用 `subprocess` + `CREATE_NEW_PROCESS_GROUP`，停止用 `taskkill /T /F`
- [ ] 状态探测：用"哪份 `.log` 最新"认领当前探针版本（Linux 版已这么做），Windows 侧路径换成 `%LOCALAPPDATA%` 或项目目录

## 待验证清单（到 Win11 上逐条打勾）

- [ ] `use_mobile_platform` 是否直接给出移动端 UI
- [ ] 探针在 Win11 版原神上的 RVA 是否需要重找（版本号变了就要）
- [ ] 系统 WM_TOUCH 是否被 Unity 消费（若被消费，可以不做合成，只做 UI 切换）
- [ ] 平板触摸入口：Sunshine for Windows 的虚拟触摸屏 / 系统 HID 触摸，哪个能用
- [ ] 反作弊对注入的容忍度（Linux 侧结论：launcher 起实例后注入安全，直起实例会被杀）
