# vendor/ffmpeg

这一份 ffmpeg **随安装包发出去**，用户不需要自己安装。

## 为什么放这里

插件的两件事离不开 ffmpeg，而它们都没法用别的方式替代：

| 用途 | 为什么必须 | 没它会怎样 |
|---|---|---|
| 图片段转码 | 达芬奇摆静帧恒为 150 帧且改不了，剪映里 2.4 秒的图会占满 5 秒，后面每段位置全错 | 图片一送进去时间线就整体错位 |
| 变速段预渲染 | 达芬奇脚本接口没有设速度的入口（`SetSpeed` 等 11 个候选全是 None），1x 摆放只能看到源区间的前一截 | 加速段尾部画面丢失、慢放段越界读画面 |

0.4.0 曾把它当可选依赖（有就用、没有就退回「占位对齐」），结果两种机器表现
不一样，用户看到一句「找不到 ffmpeg」也没法处理。0.4.2 起改成内置：安装包
20 MB → 57 MB，换来「装上就能用」。

## 怎么来的

```bash
python tools/fetch_ffmpeg.py          # 已有且校验通过就跳过
python tools/fetch_ffmpeg.py --force  # 重新取一份
```

取值顺序：`--src` → `COLORPASS_FFMPEG_SRC` → 已安装的 `imageio_ffmpeg` → 官方下载兜底。
拿到的文件会跑 `ffmpeg -version` / `-encoders` / `-filters` 校验，确认带
`prores_ks`、`libx264`、`atempo`、`setpts` 才收下。

## 为什么不进 Git

105 MB，超过 GitHub 的单文件上限。`manifest.json`（版本、体积、SHA256）是提交的，
换机器重建时能核对拿到的是不是同一份。

## 许可

`ffmpeg.exe` 是第三方二进制，遵循其自身许可（本仓库用的是含 libx264 的 GPL
构建）。再分发时请保留 FFmpeg 的 LICENSE 与源码获取方式，见
<https://ffmpeg.org/legal.html>。
