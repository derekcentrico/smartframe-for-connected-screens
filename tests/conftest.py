import os
import sys
import tempfile

# Config is read at import time, so point it at scratch dirs before app loads.
_ROOT = tempfile.mkdtemp(prefix="smartframe-test-")
os.environ["MEDIA_BASE"] = os.path.join(_ROOT, "media")
os.environ["CACHE_BASE"] = os.path.join(_ROOT, "cache")
os.environ.setdefault("FAVORITE_MIN_PHOTOS", "3")
os.makedirs(os.environ["MEDIA_BASE"], exist_ok=True)
os.makedirs(os.environ["CACHE_BASE"], exist_ok=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
