from importlib.metadata import entry_points
from pathlib import Path

import contact_forms


matches = [
    item
    for item in entry_points(group="kururucms.plugins")
    if item.name == "contact_forms"
]
if len(matches) != 1:
    raise SystemExit(f"expected one contact_forms entry point, found {len(matches)}")
if matches[0].value != "contact_forms.apps:ContactFormsConfig":
    raise SystemExit(f"unexpected entry point: {matches[0].value}")
matches[0].load()

package_root = Path(contact_forms.__file__).resolve().parent
required = [
    package_root / "migrations" / "0004_form_constraints_and_maintenance.py",
    package_root
    / "management"
    / "commands"
    / "process_contact_mail_outbox.py",
    package_root / "templates" / "contact_forms" / "block.html",
]
missing = [str(path.relative_to(package_root)) for path in required if not path.is_file()]
if missing:
    raise SystemExit(f"wheel is missing required files: {missing}")
print(f"verified kururucms-contact-forms {contact_forms.__version__}")
