from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / '.native-server'
VERSION = STATE / 'public/version.json'
