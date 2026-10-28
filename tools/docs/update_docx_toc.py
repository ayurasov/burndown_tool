"""Заполнить оглавление DOCX (поле TOC) номерами страниц через LibreOffice (UNO).

pandoc вставляет оглавление как поле, которое Word заполняет только после «Обновить поле»,
а другие редакторы показывают пустым. Скрипт открывает документ в LibreOffice без
интерфейса, обновляет указатели и сохраняет DOCX.

    python3 tools/docs/update_docx_toc.py docs/manuals/user-guide.docx [...]
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import uno
from com.sun.star.beans import PropertyValue


def prop(name, value):
    p = PropertyValue()
    p.Name, p.Value = name, value
    return p


def main(paths):
    profile = tempfile.mkdtemp(prefix="lo-profile-")
    port = 2002 + os.getpid() % 1000
    office = subprocess.Popen([
        "soffice", "--headless", "--invisible", "--nologo", "--norestore",
        f"-env:UserInstallation=file://{profile}",
        f"--accept=socket,host=127.0.0.1,port={port};urp;",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        resolver = uno.getComponentContext().ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", uno.getComponentContext())
        for _ in range(60):
            try:
                ctx = resolver.resolve(f"uno:socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext")
                break
            except Exception:  # noqa: BLE001 — офис ещё стартует
                time.sleep(0.5)
        else:
            raise RuntimeError("LibreOffice не запустился")
        desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        for path in paths:
            url = uno.systemPathToFileUrl(str(Path(path).resolve()))
            doc = desktop.loadComponentFromURL(url, "_blank", 0, (prop("Hidden", True),))
            indexes = doc.getDocumentIndexes()
            count = indexes.getCount()
            for i in range(count):
                indexes.getByIndex(i).update()
            doc.storeToURL(url, (prop("FilterName", "MS Word 2007 XML"),))
            doc.close(True)
            print(f"  toc: {path} (указателей: {count})")
    finally:
        office.terminate()
        office.wait(timeout=30)


if __name__ == "__main__":
    main(sys.argv[1:])
