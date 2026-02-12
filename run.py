#!/usr/bin/env python3
"""
Startup script for the Anti-Impersonator Bot.
This handles the Python path correctly so you can run it from anywhere.
"""
import sys
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Now import and run the main module
from src.main import main

if __name__ == '__main__':
    main()
