# podcast-elbarto

手动制作播客的本地项目。文章由你放进 `sources/`，项目不会读取 X 收藏，也不会定时运行或触发 GitHub Actions。

## 准备

1. 将 UTF-8 编码的 `.md` 或 `.txt` 文章放入 `sources/`。文章内容默认被 Git 忽略，不会随公开仓库提交。
2. 安装依赖：`python -m pip install -r requirements-podcast.txt`、`npm ci --prefix podcast`、`npx --prefix podcast playwright install chromium`。音频检查还需要本机 `ffmpeg` 和 `ffprobe`。
3. 在本机准备可用的 Pi 登录状态，通过 `PI_PROFILE` 或 `PI_STORAGE_STATE` 提供给 Playwright。音频捕获需要你手动运行命令，遇到验证或登录问题会停止。

## 制作一篇

```sh
python podcast_import.py sources/example.md
python podcast_run.py --task <上一步输出的任务 ID>
```

导入同一个文件不会重复入队；修改文章后重新导入会建立新版本。`podcast_run.py` 生成音频后停在 `needs_review`。请检查 `podcast-work/<任务 ID>/` 中的文章、稿件、音频和 `quality.json`。如需恢复已有捕获，显式运行 `python podcast_run.py --resume --task <任务 ID>`，先核对 Pi 中的回复，避免重复提交。

`quality.json` 中的 `source_to_adapted` 和 `adapted_to_audio` 均需独立填写 `approved`、`reviewer`、`evidence` 后，才能手动执行 `python podcast_run.py --publish-ready --task <任务 ID> --store local`。发布前还需设置 `PODCAST_BASE_URL`；本地输出在 `podcast-store/`。这个命令不会自行上传到播客平台。

## 验证

```sh
python -m unittest discover -s tests -p 'test_podcast*.py' -b
npm test --prefix podcast
```
