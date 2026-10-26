import os, sys
from playwright.sync_api import sync_playwright
here = os.path.dirname(os.path.abspath(__file__))
out = sys.argv[1]
footer = ('<div style="width:100%;font-family:Inter,DejaVu Sans,sans-serif;font-size:8px;color:#8a929b;'
          'padding:0 16mm;display:flex;justify-content:space-between;">'
          '<span>Burndown — руководство пользователя</span>'
          '<span>стр. <span class="pageNumber"></span> из <span class="totalPages"></span></span></div>')
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH') or None)
    pg = b.new_page()
    pg.goto('file://' + os.path.join(here, 'manual.html'))
    pg.wait_for_load_state('load')
    pg.evaluate('document.fonts.ready')
    # проход 1: узнаём страницы глав, проставляем их в оглавление
    pg.pdf(path=out, format='A4', print_background=True, prefer_css_page_size=True)
    from pypdf import PdfReader as _R
    texts = [pp.extract_text().lstrip() for pp in _R(out).pages]
    chapters = pg.evaluate("[...document.querySelectorAll('section.chapter[id]')].map(s => [s.id, s.querySelector('h1').textContent])")
    pages = {}
    for cid, title in chapters:
        for i, t in enumerate(texts):
            if i > 1 and t.startswith(title):
                pages[cid] = i + 1
                break
    pg.evaluate("p => document.querySelectorAll('.toc .pg').forEach(el => { el.textContent = p[el.dataset.for] || ''; })", pages)
    # проход 2: окончательный файл
    pg.pdf(path=out, format='A4', print_background=True, prefer_css_page_size=True,
           display_header_footer=True, header_template='<span></span>', footer_template=footer)
    cover = out + '.cover.pdf'
    pg.pdf(path=cover, format='A4', print_background=True, prefer_css_page_size=True, page_ranges='1')
    b.close()

# обложка — без колонтитула
from pypdf import PdfReader, PdfWriter
body, first = PdfReader(out), PdfReader(cover)
w = PdfWriter()
w.add_page(first.pages[0])
for page in body.pages[1:]:
    w.add_page(page)
# закладки по главам: первая страница, текст которой начинается с заголовка главы
import re
titles = re.findall(r'<section class="chapter[^"]*"[^>]*>\s*<h1>(.*?)</h1>', open(os.path.join(here, 'manual.html'), encoding='utf-8').read())
for title in titles:
    for i, page in enumerate(w.pages):
        if page.extract_text().lstrip().startswith(title):
            w.add_outline_item(title, i)
            break
w.add_metadata({'/Title': 'Burndown — руководство пользователя', '/Subject': 'Руководство пользователя трекера сгорания работ'})
with open(out, 'wb') as f:
    w.write(f)
os.remove(cover)
print('ok', out)
