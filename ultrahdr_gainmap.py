"""Sigal plugin: preserve Ultra HDR / Adobe gain-map JPEGs through resize.

Pillow opens a gain-map JPEG (an MPO container: an SDR base image plus an
auxiliary gain-map image, per Google's Ultra HDR format or Adobe Lightroom's
HDR export) without error, but silently drops the gain map on save -- any
resize produces a flattened SDR JPEG with no warning. See
https://github.com/python-pillow/Pillow/issues/8036.

This plugin detects gain-map JPEGs and, for those only, resizes the base
image and the gain map separately with Pillow (using sigal's own
img_size/img_processor/jpg_options settings) and remerges them with Google's
``libultrahdr`` (the ``ultrahdr_app`` CLI), producing a correctly resized
JPEG that still carries a valid gain map. Every other image is untouched and
goes through sigal's normal Pillow-only pipeline.

Requires the ``exiftool`` and ``ultrahdr_app`` (``brew install
libultrahdr``) binaries on PATH. If either is missing, the plugin logs a
warning and never connects, so sigal's default image processing runs
unchanged (gain maps silently flattened, as without this plugin).

Enable it from ``sigal.conf.py`` (never edit the installed ``sigal``
package)::

    plugin_paths = ["/path/to/sigal-ultrahdr-gainmap"]
    plugins = ["ultrahdr_gainmap"]
"""

import logging
import multiprocessing
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pilkit.processors
from PIL import Image
from pilkit.utils import save_image
from sigal import signals
from sigal.image import generate_image, generate_thumbnail
from sigal.utils import raise_if_debug

logger = logging.getLogger(__name__)

#: Generous but bounded; a hung exiftool/ultrahdr_app call shouldn't stall a build.
_SUBPROCESS_TIMEOUT = 60


def _has_gain_map(src_path: str) -> bool:
    """Whether a JPEG carries a gain map, per ``ultrahdr_app`` (authoritative).

    No cheaper Pillow-based pre-filter: Pillow tags some genuine gain-map
    JPEGs as plain "JPEG" rather than "MPO" (observed on a Lightroom Classic
    15.5 export that an earlier 14.2 export didn't trigger), so gating on
    Pillow's format string silently missed real gain maps. ~10ms/call here
    is an acceptable cost for correctness.
    """
    try:
        result = subprocess.run(
            ["ultrahdr_app", "-m", "1", "-j", src_path, "-P"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "Ultra HDR Image: Yes" in result.stdout


def _resized_size(img: Image.Image, settings: dict[str, Any]) -> tuple[int, int]:
    """Target (width, height), swapped for portrait images like sigal's own."""
    width, height = settings["img_size"]
    if img.size[0] < img.size[1]:
        return height, width
    return width, height


def _resize_and_save(
    src_path: str, dst_path: str, settings: dict[str, Any], options: dict[str, Any]
) -> None:
    """Resize one JPEG with sigal's configured processor and save it.

    Gain maps are often stored well below img_size (sometimes downsampled
    grayscale, e.g. 645x857 vs. a 1152x1100 target); with upscale=False,
    pilkit's processor then returns the *same* image object rather than a
    copy. save_image() must run before the file closes, or converting that
    now-fp-less image (e.g. grayscale -> RGB) fails with "'NoneType' object
    has no attribute 'seek'" / "Operation on closed image".
    """
    processor_cls = getattr(pilkit.processors, settings["img_processor"])
    with Image.open(src_path) as img:
        width, height = _resized_size(img, settings)
        resized = processor_cls(width, height, upscale=False).process(img)
        save_image(resized, dst_path, "JPEG", options=options, autoconvert=True)


def _merge_gain_map(media: Any, settings: dict[str, Any]) -> None:
    """Resize the base image and its gain map separately, then remerge them.

    Raises on any failure; the caller falls back to sigal's normal
    (SDR-flattening) resize so one bad file can't break the whole build.
    """
    options = settings["jpg_options"]
    with TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        gainmap_src = tmp / "gainmap.jpg"
        metadata_cfg = tmp / "metadata.cfg"
        base_out = tmp / "base.jpg"
        gainmap_out = tmp / "gainmap_resized.jpg"

        with gainmap_src.open("wb") as fh:
            subprocess.run(
                ["exiftool", "-b", "-MPImage2", media.src_path],
                stdout=fh,
                check=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
        subprocess.run(
            [
                "ultrahdr_app",
                "-m",
                "1",
                "-j",
                media.src_path,
                "-f",
                str(metadata_cfg),
            ],
            check=True,
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )

        _resize_and_save(media.src_path, str(base_out), settings, options)
        _resize_and_save(str(gainmap_src), str(gainmap_out), settings, options)

        subprocess.run(
            [
                "ultrahdr_app",
                "-m",
                "0",
                "-i",
                str(base_out),
                "-g",
                str(gainmap_out),
                "-f",
                str(metadata_cfg),
                "-z",
                media.dst_path,
            ],
            check=True,
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )


def process_gain_map_image(media: Any) -> int:
    """Resize a gain-map JPEG while preserving its HDR gain map."""
    settings = media.settings
    logger.info("Processing %s", media.src_path)

    with raise_if_debug() as status:
        try:
            _merge_gain_map(media, settings)
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning(
                "ultrahdr_gainmap: failed to preserve gain map for %s (%r), "
                "falling back to flattened SDR resize",
                media.src_path,
                e,
            )
            generate_image(
                media.src_path,
                media.dst_path,
                settings,
                options=settings["jpg_options"],
            )

        if settings["make_thumbs"]:
            generate_thumbnail(
                media.dst_path,
                media.thumb_path,
                settings["thumb_size"],
                fit=settings["thumb_fit"],
                options=settings["jpg_options"],
                thumb_fit_centering=settings["thumb_fit_centering"],
            )

    return status.value


def choose_processor(sender: Any, **kwargs: Any) -> Any:
    """process_file receiver: use the gain-map processor for gain-map JPEGs."""
    media = sender
    if (
        media.type == "image"
        and media.src_ext.lower() in (".jpg", ".jpeg")
        and _has_gain_map(media.src_path)
    ):
        return process_gain_map_image
    return None


def register(settings: dict[str, Any]) -> None:
    """Sigal entry point: connect the plugin's signal handler.

    Also forces the multiprocessing start method to "fork". Sigal builds
    images in a worker pool (gallery.py's ``init_pool``) without re-running
    plugin registration inside each worker; on macOS the default "spawn"
    method starts each worker as a fresh interpreter, so this plugin's
    ``process_file`` receiver would silently never run there and every image
    would fall through to sigal's default (SDR-flattening) processor with no
    error. "fork" makes each worker inherit this process's already-connected
    signal instead. Must happen here, before sigal's ``Gallery.__init__``
    creates the pool.
    """
    if shutil.which("exiftool") is None or shutil.which("ultrahdr_app") is None:
        logger.warning(
            "ultrahdr_gainmap: exiftool and/or ultrahdr_app (brew install "
            "libultrahdr) not found on PATH; gain-map JPEGs will be "
            "flattened to SDR like any other Pillow resize"
        )
        return
    multiprocessing.set_start_method("fork", force=True)
    signals.process_file.connect(choose_processor)
