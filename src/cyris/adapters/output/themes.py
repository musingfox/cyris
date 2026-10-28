"""The page palettes `digest.theme` reskins every rendered page with.

A theme is a palette of the colour tokens `_tokens.css.j2` declares, plus the
three colour literals `_components.css.j2` keeps outside the tokens (the
primary button's hover, the site bar's backdrop, the brand mark's glow).
`default` is not one of them: it is the tokens as written — the spec's own
palette stays the one pages carry, and `theme_style` answers "" for it so the
writer emits nothing. Palettes only answer colours; sizes, fonts, radii and
`--type-scale` belong to the page, not the palette. Colour keeps the two
meanings the spec gives it (docs/design/ui-language.md §5): `--accent` what the
reader voted up, `--warn` what they voted down, everything else neutral.
"""

from __future__ import annotations

# The choices /settings offers, in its order, by the `digest.theme` value each
# option stores; palettes below answer every id except "default".
THEMES: tuple[tuple[str, str], ...] = (
    ("default", "Default"),
    ("light", "Light"),
    ("one_dark", "One Dark"),
    ("catppuccin", "Catppuccin"),
    ("nord", "Nord"),
    ("tokyo_night", "Tokyo Night"),
    ("github_dark", "GitHub Dark"),
)

# fmt: off
PALETTES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#fcfcfc", "bg-elev": "#ffffff", "surface": "#f3f4ef", "surface-2": "#e9eae3",
        "border": "#dcded4", "border-strong": "#c6c9bb",
        "text": "#191c14", "text-dim": "#5c6050", "text-faint": "#8f927f",
        "accent": "#3f7a00", "accent-dim": "#7ba636", "warn": "#c7174c",
    },
    "one_dark": {
        "bg": "#282c34", "bg-elev": "#2c313c", "surface": "#2c313c", "surface-2": "#31353f",
        "border": "#3e4451", "border-strong": "#4b5263",
        "text": "#dcdfe4", "text-dim": "#9da5b4", "text-faint": "#5c6370",
        "accent": "#98c379", "accent-dim": "#6c9e4f", "warn": "#e06c75",
    },
    "catppuccin": {
        "bg": "#1e1e2e", "bg-elev": "#181825", "surface": "#181825", "surface-2": "#313244",
        "border": "#45475a", "border-strong": "#585b70",
        "text": "#cdd6f4", "text-dim": "#a6adc8", "text-faint": "#6c7086",
        "accent": "#a6e3a1", "accent-dim": "#77c47f", "warn": "#f38ba8",
    },
    "nord": {
        "bg": "#2e3440", "bg-elev": "#333b4a", "surface": "#3b4252", "surface-2": "#434c5e",
        "border": "#4c566a", "border-strong": "#5e6a81",
        "text": "#eceff4", "text-dim": "#bcc2cd", "text-faint": "#7b88a1",
        "accent": "#a3be8c", "accent-dim": "#76a05f", "warn": "#bf616a",
    },
    "tokyo_night": {
        "bg": "#1a1b26", "bg-elev": "#1f2335", "surface": "#24283b", "surface-2": "#2f334d",
        "border": "#3b4261", "border-strong": "#4f587e",
        "text": "#c0caf5", "text-dim": "#9aa5ce", "text-faint": "#565f89",
        "accent": "#9ece6a", "accent-dim": "#73a846", "warn": "#f7768e",
    },
    "github_dark": {
        "bg": "#0d1117", "bg-elev": "#161b22", "surface": "#161b22", "surface-2": "#21262d",
        "border": "#30363d", "border-strong": "#444c56",
        "text": "#e6edf3", "text-dim": "#9198a1", "text-faint": "#6e7681",
        "accent": "#3fb950", "accent-dim": "#2ea043", "warn": "#f85149",
    },
}
# fmt: on

# (accent, warn) per palette as "r, g, b", for the tints, the grid and the
# glow, which are alphas of the full-strength colour.
_RGB: dict[str, tuple[str, str]] = {
    "light": ("63, 122, 0", "199, 23, 76"),
    "one_dark": ("152, 195, 121", "224, 108, 117"),
    "catppuccin": ("166, 227, 161", "243, 139, 168"),
    "nord": ("163, 190, 140", "191, 97, 106"),
    "tokyo_night": ("158, 206, 106", "247, 118, 142"),
    "github_dark": ("63, 185, 80", "248, 81, 73"),
}


def theme_style(theme: str) -> str:
    """The CSS text that reskins a page in `theme`, or "" for the default.

    One mechanism for four page templates: the tokens overridden as a later
    `:root` rule, and the three outside-the-tokens literals resaid as later
    rules of their own selectors — later same-specificity declarations win,
    which is the whole override. Kept as text, not template partials, so the
    default palette stays spec-pinned byte for byte (tests compare it whole).
    """
    tokens = PALETTES.get(theme)
    if not tokens:
        return ""
    accent_rgb, warn_rgb = _RGB[theme]
    bg_rgb = ", ".join(str(int(tokens["bg"][i : i + 2], 16)) for i in (1, 3, 5))
    root = ";".join(f"--{name}:{value}" for name, value in tokens.items())
    return (
        f"html:root{{{root};"
        f"--accent-tint:rgba({accent_rgb},0.07);"
        f"--warn-tint:rgba({warn_rgb},0.12);"
        f"--grid:rgba({accent_rgb},0.035)}}"
        f".btn.primary:where(:not(:disabled)):hover{{background:{tokens['accent-dim']}}}"
        f".site-bar{{background:rgba({bg_rgb},.88)}}"
        f".brand-mark{{box-shadow:0 0 12px rgba({accent_rgb},.45)}}"
    )
