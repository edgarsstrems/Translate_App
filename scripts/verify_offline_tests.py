"""Run regression tests with external sockets and Google token refresh blocked."""
import sys
import argparse
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pattern', default='test*.py')
    args = parser.parse_args()
    with patch('socket.socket.connect', side_effect=AssertionError('External network disabled in offline tests')), \
         patch('google.auth.transport.requests.Request.__call__', side_effect=AssertionError('Google token refresh disabled in offline tests')):
        result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover('tests', pattern=args.pattern))
    raise SystemExit(0 if result.wasSuccessful() else 1)
