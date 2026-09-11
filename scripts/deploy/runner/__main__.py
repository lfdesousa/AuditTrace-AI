"""``python -m scripts.deploy.runner`` entry point.

Package-mode equivalent of the ``if __name__ == "__main__":`` guard a plain
module gets for free — a package needs an explicit ``__main__.py`` for
``python -m <package>`` to find something to run (PEP 338).
"""

import sys

from scripts.deploy.runner.orchestrator import main

if __name__ == "__main__":
    sys.exit(main())
