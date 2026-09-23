# 私有文章播客：实施与运行

本文件记录实施状态和运行边界，不把技术样本当作全文质量验收。

## 用户决定（2026-09-22，优先于原方案中的相关假设）

- 使用主账号的公开 `podcast-elbarto` 仓库和标准 Ubuntu Actions runner；不创建机器账号。
- 文章、稿件、Pi checkpoint、音频和私有订阅地址不进入 Git、公开日志或 Actions artifacts。
- 不要求“高中生读懂”。仅调整为适合自然朗读的英文，不降低技术深度、不替换概念。
- Pi 输出不符合内容要求时，由 Codex 根据全文写稿并独立复核；Pi 只调整标点。字词、数字或顺序发生变化时不发布。
- 不购买付费服务；现有 Kindle 流程和博客流程不变。

## 组件

`podcast_bookmarks.py` 独立保存原文并排队，不发邮件、不发布博客。
`podcast_run.py` 一次处理一篇，显式恢复，保留两道独立质量门禁。
`podcast/pi_article.mjs` 按段落完整分组；每段有实际 Pi 回复 checkpoint；单段过长则停止而非截断。
`podcast_sync.py` 将白名单文本状态保存到私有 R2 ZIP，单活动任务的媒体单独暂存。
`podcast_publish.py` 保存稳定 GUID、发布日志和最多 20 集媒体；第 21 集上传前撤下并删除最旧音频。
`podcast/worker.mjs` 提供带私有地址令牌的 RSS、HEAD 和 Range 媒体请求。

正式主路径使用桶级 S3 凭据。另有受独立 Bearer 令牌保护、限定本桶白名单对象的 HTTP Store 适配器；它不是 Cloudflare 账号管理 API，订阅令牌不能写入存储。

## 云端配置

Workflow 支持手动 `probe/automatic/capture/resume/publish`，实际处理要求公开仓库且 `PODCAST_CLOUD_VALIDATION_ENABLED=true`。
定时另需 `PODCAST_AUTOMATION_ENABLED=true`；默认关闭。时间为 UTC+8 19:00，以 2026-09-22 为锚点每三天一次（每日轻量日期门控），GitHub 可能延迟执行。
`probe` 只验证云端 Pi 输入框可用，不发送文章、不操作业务存储；探测失败不得打开定时开关。

`automatic` 先探测 Pi，再恢复私有状态、扫描收藏，一轮最多处理一篇。仅可确认英文的纯文本自动使用完整原文作为稿件，Pi 仅调整标点；图片、代码、表格、词语变化、ASR 不完全对应或已有待处理任务都会停止发布。自动审核不是人工听审，也不表示已完成 Apple 实机验收。
独立转写采用本地开源模型，不调用付费 API。模型只从公开模型库下载；文章和音频不上传到转写服务。

GitHub Secrets：

- `X_SESSION_JSON`：既有 X Playwright storage state。
- `PI_STORAGE_STATE_JSON`：正常初始化的 Pi storage state；迁移到新浏览器或云端可能被挑战阻止。
- `PODCAST_R2_ACCESS_KEY_ID`、`PODCAST_R2_SECRET_ACCESS_KEY`：仅播客桶的 Object Read & Write。
- `PODCAST_BASE_URL`：Worker 地址加 `/p/<订阅令牌>`，不能公开。

Worker Secrets：`FEED_TOKEN`；仅使用 HTTP Store 时还需独立的 `STORE_TOKEN`。
不要把广泛 Cloudflare OAuth/刷新令牌复制进仓库或 Actions。

Python 和 Node 使用不同版本的 Playwright，workflow 分别安装对应浏览器；FFmpeg 显式安装。
任务并发固定为 `podcast-single-writer`。不要在云端任务运行时从另一台主机写入同一存储。

```sh
# 在配置好对应私有环境变量后运行；这些命令不打印私密正文。
python podcast_cloud.py capture
python podcast_cloud.py resume --task <task-id>
python podcast_cloud.py publish --task <task-id>
```

`publish` 不自动批准稿件：`quality.json` 两道检查都必须有 reviewer/evidence，且原文、稿件、音频哈希与实际文件相符。
人工修稿应使用新的明确处理版本/输出目录，不能覆盖既有提交 checkpoint 冒充重试。

Pi 改写未通过内容验收时，先由 Codex 根据原文编写朗读稿，再由独立复审确认事实、术语、数字、条件和完整性。复审 JSON 必须包含 `approved`、`reviewer`、`evidence`、原文文件的 `source_sha256` 和稿件的 `script_sha256`。

```sh
python podcast_author.py prepare --task <原任务ID> --script <私有稿件.md> --review <私有复审.json>
```

该命令创建新处理版本，保留旧 checkpoint；新版本只请求 Pi 调整标点，不自动批准发布。逐词门禁拒绝增删、改词、数字和顺序变化。仅对白名单常用词允许由句界变化引起的首字母大小写调整，且记录 `caseAdjustments`；专名和缩写不放宽。最终仍分别检查原文到稿件、稿件到音频。

## 故障与质量

- 提交前先持久保存 `submitting`，取得实际回复后保存其 ID。状态不确定时禁止盲目重发。
- 403、验证码、登录异常：标记需要处理，不绕过挑战，也不自动轮换账号或换声音。
- Headless Chromium 和 Edge 的本机迁移候选曾返回挑战；持久 Edge 本机成功不证明 GitHub 云端成功。
- 音频完整解码与时长检查只证明媒体部分属性，不证明全文语义正确。
- `podcast_transcribe.py` 用本地开源 Whisper 独立转写，输出差异证据，永不自动批准。
- 默认受控存储 1 GB 上限，正式媒体最多 20 集。账户的其他桶、请求和其他 Actions 存储仍需独立核对，不宣称整个账户零费用。
- 暂存音频运行时 24 小时到期，R2 `podcast/staging/` 另配置一天生命周期规则兜底；对象生命周期实际执行可能有延迟。
- 文本状态展开上限 20 MiB，达到上限暂停，不删减原文或悄悄丢任务。
- 令牌轮换时必须同时更新 Worker、GitHub 的 `PODCAST_BASE_URL`，然后由 publisher 用新地址重新生成 RSS；旧 enclosure 不会自动变更。

## 本地验证

```sh
python -m pip install -r requirements-podcast.txt
npm ci --prefix podcast
python -m unittest discover -s tests -b
npm test --prefix podcast
```

可选本地 ASR：安装 `requirements-podcast-review.txt`，执行
`python podcast_transcribe.py <audio.mp3> --adapted <script.md> --output <private-report.json>`。
首次运行会下载开源模型；音频不上传到第三方转写 API。报告含完整正文，应仅放在私有工作目录。

只把私有 RSS 地址直接添加到播客客户端，不向公开节目目录投稿。持有地址即有读取权限，不是 Apple 账号级 DRM。

## 临时交互登录（仅人工触发）

`podcast-login.yml` 创建最长 30 分钟的云端可视浏览器，使用短时 HTTPS Tunnel、随机访问令牌及独立 Secure/HttpOnly/SameSite Cookie。VNC 和门户仅监听 loopback；没有公网调试端口或终端。用户亲自登录/处理验证并点击保存，程序不会自动操作登录页面。

仅导出 Pi/Inflection 域的浏览器状态（含 IndexedDB），以 AES-256-GCM 加密后暂存 `podcast/login/<run-id>/state.enc`。密钥使用独立 `PODCAST_LOGIN_KEY` Secret；入口元信息同样加密，公开日志不含访问地址、令牌、截图或会话。密文绑定运行 ID、类型和有效期；该前缀另设一天对象生命周期兜底。登录态导入 GitHub Secret 后删除传输对象。

首次人工登录成功后，仍须用新的 Actions 任务执行 `probe` 验证认证复用。不得因保存状态就开启定时；若再次需要验证，保持自动化关闭。不宣称永久登录，也不自动绕过挑战。
