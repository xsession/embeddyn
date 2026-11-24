# Rebuild these docs and figures

The site is built with [MkDocs Material](https://squidfunk.github.io/mkdocs-material/) from `docs/` and `mkdocs.yml`. Every plot is drawn from a real run of the examples, and every Lab screenshot is taken from the running Lab, by scripts in `tools/docs/`.

## Preview the site

```bash
pip install -e ".[docs]"
mkdocs serve                 # http://127.0.0.1:8000, reloads on save
mkdocs build                 # static site in site/ (strict: broken links fail the build)
```

## Regenerate the plots

```bash
export RENODE_BIN=/path/to/renode                       # Renode ≥ 1.17 for the firmware figures
export NGSPICE_LIBRARY=/path/to/libngspice.so.0
tools/docs/make_figure_data.sh /tmp/doc-figures          # ~10 min with 4 workers
python tools/docs/make_figures.py /tmp/doc-figures       # writes docs/images/tutorial-*.png, theory-*.png
```

`make_figure_data.sh` runs the examples, both tuning methods, the tolerance analysis and the estimation, and saves CSV/JSON. `make_figures.py` draws from that data (and runs a few cheap Python-only simulations itself). A figure whose data is missing (no Renode, say) is skipped with a note, so the rest can still be refreshed.

## Retake the Lab screenshots

```bash
embeddyn mechatronics gui --port 8710 &
GECKODRIVER=/snap/bin/geckodriver python tools/docs/capture_lab.py http://127.0.0.1:8710
```

`capture_lab.py` drives a headless Firefox through Selenium: it opens the example projects, runs them, switches views and saves `docs/images/lab-*.png` at 1600×980. Absolute paths on screen are rewritten relative to the embeddyn root before each shot.

## Writing conventions

* **Tutorials** are step-by-step and end with a result the reader can check. **Theory** pages explain how and why, with equations. **Guides** solve one task. **Reference** pages list every option.
* Numbers in the text come from the runs that produced the figures; if you rerun with changed models, update both.
* Equations use `\( … \)` and `\[ … \]` (MathJax); diagrams use Mermaid fences.
