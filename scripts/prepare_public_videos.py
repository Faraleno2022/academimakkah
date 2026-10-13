"""Prépare les vidéos publiques en HLS adaptatif et MP4 économique avec FFmpeg.

Les originaux ne sont jamais réencodés en place. L'application sert uniquement
les fichiers produits ; FFmpeg n'est pas nécessaire sur le serveur web.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SPOT = ROOT / 'static' / 'spot'
PROFILES = (
    ('360p', 360, 28, '220k', '440k'),
    ('480p', 480, 27, '450k', '900k'),
    ('720p', 720, 26, '1000k', '2000k'),
)
SOURCES = {'presentation': 'Spot_MySchool.mp4', 'notes': 'Spot_MySchool_Notes.mp4'}


def run(args, cwd=None):
    subprocess.run(args, check=True, cwd=cwd)


def probe(path):
    return json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_streams', '-show_format', '-show_data',
        '-of', 'json', str(path),
    ], text=True, encoding='utf-8'))


def codec_string(stream):
    # Les trois octets de profil/contraintes/niveau viennent de l'avcC réel.
    first_line = next(line for line in stream['extradata'].splitlines() if ':' in line)
    data = bytes.fromhex(first_line.split(':', 1)[1].strip().split('  ', 1)[0])
    return 'avc1.' + data[1:4].hex() + ',mp4a.40.2'


def hls_metrics(playlist):
    durations = [float(n) for n in re.findall(r'#EXTINF:([0-9.]+)', playlist.read_text())]
    segments = [playlist.parent / line.strip() for line in playlist.read_text().splitlines()
                if line.strip() and not line.startswith('#')]
    assert len(segments) == len(durations) and segments
    sizes = [segment.stat().st_size for segment in segments]
    return {
        'segments': len(segments),
        'duree': sum(durations),
        'octets': sum(sizes) + (playlist.parent / 'init.mp4').stat().st_size,
        'debit_moyen': math.ceil(sum(sizes) * 8 / sum(durations)),
        'debit_maximum': math.ceil(max(size * 8 / duration for size, duration in zip(sizes, durations)) * 1.1),
        'segment_max_octets': max(sizes),
    }


def prepare(slug, filename, overwrite):
    source = SPOT / filename
    output = SPOT / 'stream-v1' / slug
    if not output.resolve().is_relative_to(SPOT.resolve()):
        raise ValueError('Destination hors du dossier des vidéos.')
    if output.exists() and any(output.iterdir()) and not overwrite:
        raise SystemExit(f'{output} existe déjà ; utiliser --overwrite pour les seuls fichiers générés.')
    output.mkdir(parents=True, exist_ok=True)
    source_info = probe(source)
    with source.open('rb') as original:
        checksum = hashlib.file_digest(original, 'sha256').hexdigest()
    report = {'source': filename, 'source_octets': source.stat().st_size,
              'source_sha256': checksum,
              'duree': float(source_info['format']['duration']), 'profils': {}}
    master = ['#EXTM3U', '#EXT-X-VERSION:7', '#EXT-X-INDEPENDENT-SEGMENTS']
    with tempfile.TemporaryDirectory(prefix='public-video-', dir=ROOT / 'tmp') as scratch:
        for name, height, crf, maxrate, bufsize in PROFILES:
            print(f'{slug} : encodage {name}...', flush=True)
            mp4 = output / 'economique.mp4' if name == '360p' else Path(scratch) / f'{name}.mp4'
            run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source),
                 '-map', '0:v:0', '-map', '0:a:0', '-map_metadata', '-1',
                 '-vf', f'fps=24,scale=-2:{height},setsar=1',
                 '-c:v', 'libx264', '-preset', 'medium', '-crf', str(crf),
                 '-maxrate', maxrate, '-bufsize', bufsize, '-profile:v', 'main', '-level:v', '3.1',
                 '-pix_fmt', 'yuv420p', '-g', '48', '-keyint_min', '48', '-sc_threshold', '0',
                 '-c:a', 'aac', '-b:a', '48k', '-ar', '48000', '-ac', '1',
                 '-movflags', '+faststart', str(mp4)])
            rendition = output / name
            rendition.mkdir(exist_ok=True)
            run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(mp4),
                 '-map', '0:v:0', '-map', '0:a:0', '-c', 'copy', '-f', 'hls',
                 '-hls_time', '2', '-hls_playlist_type', 'vod', '-hls_segment_type', 'fmp4',
                 '-hls_flags', 'independent_segments', '-hls_fmp4_init_filename', 'init.mp4',
                 '-hls_segment_filename', 'seg_%05d.m4s', 'index.m3u8'], cwd=rendition)
            info = probe(mp4)
            video = next(stream for stream in info['streams'] if stream['codec_type'] == 'video')
            metrics = hls_metrics(rendition / 'index.m3u8')
            metrics.update(largeur=video['width'], hauteur=video['height'], codecs=codec_string(video))
            report['profils'][name] = metrics
            master.extend([
                f'#EXT-X-STREAM-INF:BANDWIDTH={metrics["debit_maximum"]},'
                f'AVERAGE-BANDWIDTH={metrics["debit_moyen"]},'
                f'RESOLUTION={video["width"]}x{video["height"]},FRAME-RATE=24.000,CODECS="{metrics["codecs"]}"',
                f'{name}/index.m3u8',
            ])
            print(f'{slug} {name} : {metrics["octets"] / 1000000:.2f} Mo, '
                  f'{metrics["debit_moyen"] / 1000:.0f} kbit/s, {metrics["segments"]} segments', flush=True)
    (output / 'master.m3u8').write_text('\n'.join(master) + '\n', encoding='utf-8')
    report['economique_octets'] = (output / 'economique.mp4').stat().st_size
    report['reduction_pourcentage'] = round(100 * (1 - report['economique_octets'] / report['source_octets']), 1)
    (output / 'encodage.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--overwrite', action='store_true')
    parser.add_argument('--video', choices=SOURCES)
    args = parser.parse_args()
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise SystemExit('FFmpeg et FFprobe doivent être installés sur la machine de préparation.')
    (ROOT / 'tmp').mkdir(exist_ok=True)
    for slug, filename in SOURCES.items():
        if args.video is None or args.video == slug:
            print(json.dumps(prepare(slug, filename, args.overwrite), ensure_ascii=False), flush=True)
