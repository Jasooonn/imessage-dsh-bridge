# iMessage ↔ DeepSeek Harness Bridge

在自己的 Mac 上，用一个指定的 iMessage 私聊向 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 发送 `/ds` 任务，并收取确认、进度、结果和文件。项目使用本地 [`imsg`](https://github.com/openclaw/imsg) 命令行工具；不提供云端转发服务，也不是 DeepSeek 或 Apple 的官方产品。

> **安全提示**：这相当于把一个可操作本机文件的 AI Agent 接到消息入口。请只允许你自己控制的号码，先在隔离工作区测试，不要把它用于不受信任的群聊或公开号码。关键词确认只是辅助防护，不是完整沙箱或权限系统。

## 功能

- 只处理指定发件人的一对一 iMessage；忽略群聊、自己发出的消息和没有 `/ds ` 前缀的消息。
- 串行运行 DeepSeek Harness 的 `headless` profile，先确认收到，再发送进度和分段结果。
- 普通任务续接同一 DSH 聊天；只有显式发送 `/ds new mission` 才为后续任务新建聊天。会话与任务产物保存在配置的桥接工作区。
- 支持图片、文档等入站附件；若 iMessage 尚未下载完成，按原消息 ID 重查，最多等待 120 秒。
- 支持模型把允许类型的文件放入任务发件目录，再回传到同一个 iMessage 会话；发送失败的文件可重试。
- 支持暂停、恢复、急停、流式输出开关、状态与交互指南；控制设置在本地 SQLite 中持久化。
- 按消息 GUID 去重；每个任务使用独立的项目目录和附件目录。

## 使用方式

```text
/ds 检查工作区中的测试失败原因，只分析不要修改
```

图片或文件需要和 `/ds ` 开头的说明文字放在同一条消息里，例如 `/ds 概括附件`。

| 指令 | 作用 |
| --- | --- |
| `/ds status` 或 `/ds states` | 查看状态，并附交互指南 |
| `/ds help` | 查看交互指南 |
| `/ds pause` | 暂停开始后续任务；当前任务继续 |
| `/ds resume` | 恢复执行 |
| `/ds stop` | 急停当前任务及其子进程、取消排队，保持暂停 |
| `/ds new mission` | 已排队任务执行完后切换聊天；下一条任务新建会话，之后继续该会话 |
| `/ds stream on` / `/ds stream off` | 开关增量文字回传 |
| `/ds files retry <8位任务编号>` | 重试发送失败的文件 |

高风险关键词会要求一次性确认码；它不能识别所有危险操作。`/ds stop` 后需用 `/ds resume` 才会继续。

桥接器把当前 `headless` 会话 ID 存入本地 SQLite，重启后仍会尝试续接。若该会话丢失、移出桥接工作区，或被桌面 App 占用写锁，任务会报错，桥接器不会擅自新开聊天。桌面 App 自建的 `standard` 会话不能被 `headless` CLI 续接；要开始另一段桥接对话，请显式发送 `/ds new mission`。桌面 App 可能改写工作区索引；桥接器会在任务完成时重新登记当前会话，但请以实际界面为准。

## 环境要求

- 一台已登录 iMessage 的 macOS 电脑。
- Python 3、Swift 编译器（Xcode Command Line Tools）、`imsg`；推荐安装 `zstd` 以将会话登记到桌面工作区。
- 已安装并完成凭据配置的 DeepSeek Harness 桌面 App。默认示例配置调用 `/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh`，而不是可能与桌面版凭据格式不兼容的旧全局 npm CLI。
- 给 `imsg` 及下述附件 Helper 必需的 macOS“完全磁盘访问权限”；需要时允许 Messages 自动化。权限由你在系统设置中手动授予，本项目不会绕过系统提示。

此项目在 macOS、`imsg` 0.15.8 和 DeepSeek Harness 桌面 CLI 0.2.0-rc.2 上完成过测试。其他版本的命令、输出和权限行为可能不同。

## 安装

1. 克隆仓库，并在项目目录运行 `./build-attachment-helper.sh`。脚本在 `~/Applications` 创建本地 Helper App，**不会**替你授予系统权限。到“系统设置 → 隐私与安全性 → 完全磁盘访问权限”中按需授权。
2. 将 `config.example.json` 复制为 `config.json`。填入你自己控制的手机号（含国家区号）或 Apple ID 邮箱，将 `enabled` 改为 `true`。`config.json` 已列入 `.gitignore`，不要提交它。
3. 根据实际安装位置检查 `imsg` 和桌面 CLI 路径。`imsg` 留空时会从 `PATH` 查找；如果 LaunchAgent 找不到，请填绝对路径。
4. 创建 `workspace` 指定的目录，并在 DeepSeek Harness 桌面 App 的“添加工作区”中选择它。任务产物位于 `projects/<任务编号>/`。
5. 先验证配置，再安装后台服务：

```bash
/usr/bin/python3 bridge.py --config config.json --check
./install-launch-agent.sh
```

安装脚本会将运行副本置于 `~/Library/Application Support/iMessageDSHarness`，生成当前用户的 LaunchAgent，并启动服务；不会把 API Key 写入配置或环境变量。Harness 自己负责读取凭据。

如需停止并移除自启动项：

```bash
./uninstall-launch-agent.sh
```

卸载脚本仅停止服务并将 LaunchAgent 配置移到废纸篓；源码、任务库和附件不会自动删除。

## 文件与安全边界

- 入站附件由单独的 Swift Helper 从 Messages 附件目录复制到 `~/Library/Application Support/iMessageDSHarness/inbox/<任务编号>/`，副本权限为 `600`。Helper 只接受这两个固定目录之间的复制。
- 默认最多 5 个入站附件，单个 25 MB，总计 50 MB。程序/安装包、脚本和压缩包会被拒绝；附件文字被告知 AI 作为不可信数据处理。
- 入站附件副本**不会自动过期删除**；如果包含私密内容，请在任务结束后自行清理对应任务目录。
- 回传文件必须位于桥接工作区的 `.ds-outbox/<任务编号>/`；桥接器检查文件类型、大小、名称和符号链接。发送成功后删除发件副本；失败则保留供重试。
- 桥接器只向配置的同一会话回复。若允许的发件人号码或 Apple ID 被别人控制，别人也能提交任务；请保护账号并定期检查日志。
- 任务仍受你本机 DeepSeek Harness 配置、模型能力和 macOS 权限影响。本项目不保证模型判断安全，也不应处理支付、凭据或无人监督的高风险操作。

## 开发与测试

```bash
/usr/bin/python3 -m unittest discover -s tests -v
```

测试使用临时目录和模拟消息，不会向真实联系人发 iMessage。真实收发、macOS 权限和桌面 App 的兼容性仍需要在自己的设备上验证。

## License

MIT。详见 [LICENSE](LICENSE)。
