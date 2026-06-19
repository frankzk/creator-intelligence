# ECC Rules (vendored)

Reglas de calidad de código adoptadas del proyecto
[ECC — affaan-m/ECC](https://github.com/affaan-m/ECC) (MIT License, ver `LICENSE`).

- **Origen:** https://github.com/affaan-m/ECC `rules/`
- **Commit:** e8e5793bdf1f07d95860e731cdd3633ee3cdec0a
- **Subconjunto incluido:** `common/` + `python/` (el proyecto es Python/FastAPI).

## Cómo se usan

`CLAUDE.md` (en la raíz) referencia estas reglas para que Claude Code las aplique
al desarrollar este proyecto. Cada `.md` declara en su frontmatter los `paths:`
a los que aplica.

## Actualizar

```bash
git clone --depth 1 https://github.com/affaan-m/ECC.git /tmp/ECC
cp -r /tmp/ECC/rules/common /tmp/ECC/rules/python .claude/rules/ecc/
# actualizar el commit indicado arriba
```

## Otros lenguajes

Si el proyecto incorpora otro stack, copia su carpeta desde `ECC/rules/`
(p. ej. `typescript/`, `golang/`) al lado de `python/`.
