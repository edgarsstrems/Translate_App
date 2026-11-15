"""Compare a git HEAD baseline or working mixer on the same offline PCM soak.

Run each version in a fresh process. CPU/RAM measure the test process on this
computer, not a live service or local Yoruba model inference.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.runtime_metrics import ResourceSampler
from scripts import verify_shared_mixer as verifier


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args()
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    if args.baseline:
        code = subprocess.check_output(['git', 'show', 'HEAD:church_translator/audio_mixer.py'], text=True)
        module = types.ModuleType('church_translator._baseline_mixer')
        exec(compile(code, '<git HEAD mixer>', 'exec'), module.__dict__)
        verifier.MixedAudioPlayer = module.MixedAudioPlayer
    sampler = ResourceSampler()
    began, cpu = time.perf_counter(), time.process_time()
    result = verifier.two_hour_service()
    result.update({'baseline_revision': revision, 'baseline': args.baseline,
        'wall_seconds': time.perf_counter() - began, 'process_cpu_seconds': time.process_time() - cpu,
        'resources': sampler.sample(), 'resource_scope': 'fresh Python verifier process; simulated device and no Yoruba inference'})
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('ok', 'baseline', 'wall_seconds', 'process_cpu_seconds', 'resources')}))


if __name__ == '__main__':
    main()
