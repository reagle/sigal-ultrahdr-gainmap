# sigal-ultrahdr-gainmap

Sigal plugin that preserves Ultra HDR / Adobe gain-map JPEGs through resize.

Pillow (which sigal uses for all image processing) opens a gain-map JPEG
fine but silently drops the gain map on save: every resize flattens it to
plain SDR with no warning
([python-pillow/Pillow#8036](https://github.com/python-pillow/Pillow/issues/8036)).
This plugin detects gain-map JPEGs specifically and routes just those
through Google's [`libultrahdr`](https://github.com/google/libultrahdr)
instead, so the resized output keeps its gain map. Every other image is
untouched.

## Requirements

- `exiftool`
- `ultrahdr_app` from `libultrahdr` (`brew install libultrahdr`)

If either is missing from `PATH`, the plugin logs a warning at build start
and never activates; sigal falls back to its normal behavior (gain maps
silently flattened), same as without this plugin.

## Setup

```python
# sigal.conf.py
plugin_paths = ["/path/to/sigal-ultrahdr-gainmap"]
plugins = ["ultrahdr_gainmap"]
```

## How it works

For a file `ultrahdr_app -P` confirms as a real Ultra HDR gain map (this
also accepts Adobe Lightroom's HDR gain-map export, not just
Android/Google's Ultra HDR; Pillow's own format sniffing isn't used to
pre-filter, since it mistags some genuine gain-map JPEGs as plain "JPEG"
rather than "MPO"):

1. Extract the embedded gain-map JPEG stream (`exiftool -b -MPImage2`).
2. Dump the gain-map metadata (`ultrahdr_app -m 1 -j src -f metadata.cfg`).
3. Resize the base image and the gain map separately, through the same
   Pillow processor/`img_size`/`jpg_options` sigal already uses.
4. Remerge them (`ultrahdr_app -m 0 -i base -g gainmap -f metadata.cfg`).

Thumbnails are always flattened SDR (small previews don't need HDR, and
`ultrahdr_app` has no thumbnail-box resize mode anyway).

If any step fails, the file falls back to sigal's normal Pillow resize
(flattened SDR) rather than failing the whole build.
