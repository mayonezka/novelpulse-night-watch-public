"""Owner-authorized Google HTML challenge, separate from indexed content pages."""
from pathlib import Path

GOOGLE_FILENAME = 'google9ec77d0eedd0d7a9.html'
GOOGLE_CONTENT = 'google-site-verification: ' + GOOGLE_FILENAME


def google_verification_files(directory) -> tuple[str, ...]:
    root = Path(directory).resolve()
    path = root / GOOGLE_FILENAME
    if not path.exists() and not path.is_symlink():
        return ()
    if (path.is_symlink() or not path.is_file() or path.resolve().parent != root
            or path.read_bytes() != GOOGLE_CONTENT.encode('ascii')):
        raise ValueError('Google HTML verification file must match the authorized challenge exactly')
    return (GOOGLE_FILENAME,)
