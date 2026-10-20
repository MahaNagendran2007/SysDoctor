"""Start the SysDoctor dashboard:  python run_sysdoctor.py

Fixes the Windows registry problem where ".js" is served as "text/plain" (breaks Streamlit's lazily
loaded widgets with "Failed to fetch dynamically imported module") BEFORE the web server starts,
then launches `streamlit run app.py`.
"""
import mimetypes
import sys
from pathlib import Path

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")

if __name__ == "__main__":
    from streamlit.web import cli as stcli
    app = str(Path(__file__).resolve().parent / "app.py")
    sys.argv = ["streamlit", "run", app] + sys.argv[1:]
    sys.exit(stcli.main())
