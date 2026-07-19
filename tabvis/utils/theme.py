"""Color themes

Defines the six concrete :data:`Theme` palettes (``dark`` / ``light`` / ``light-daltonized`` /
``dark-daltonized`` / ``light-ansi`` / ``dark-ansi``), the :func:`get_theme` resolver, and
:func:`theme_color_to_ansi` (a theme RGB color → ANSI escape sequence, for asciichart rendering).

Behavior notes (per ``docs/SPINE_CONTRACTS.md``):
- Each ``Theme`` is a plain ``dict[str, str]`` keyed by the TS field names kept VERBATIM (e.g.
  ``systemBlue_FOR_SYSTEM_SPINNER``, ``red_FOR_SUBAGENTS_ONLY``, ``rainbow_red_shimmer``,
  ``clawd_body``). These are wire-ish style keys read across the renderer, so the casing is
  preserved exactly — only module/function identifiers are snake_case. :class:`Theme` is a
  ``TypedDict`` documenting that surface.
- ``THEME_NAMES`` / ``THEME_SETTINGS`` and the ``ThemeName`` / ``ThemeSetting`` literals are
  implemented as tuples + :data:`typing.Literal` aliases.
- ``chalk`` (npm) is not available; :func:`theme_color_to_ansi` synthesizes the SGR escapes with
  the stdlib (truecolor ``\\x1b[38;2;r;g;bm``; 256-color ``\\x1b[38;5;Nm`` for Apple Terminal,
  which doesn't handle 24-bit color well — matching ``new Chalk({ level: 2 })``). ``chalk`` is
  recorded in ``deps_needed`` as the dropped dependency.
- The Apple-Terminal 256-color gate reads :data:`tabvis.utils.env.env`'s ``terminal`` field.
"""

from __future__ import annotations

import re
from typing import Literal, TypedDict

from tabvis.utils.env import env


class Theme(TypedDict):
    autoAccept: str
    bashBorder: str
    tabvis: str
    tabvisShimmer: str
    systemBlue_FOR_SYSTEM_SPINNER: str
    systemBlueShimmer_FOR_SYSTEM_SPINNER: str
    permission: str
    permissionShimmer: str
    planMode: str
    ide: str
    promptBorder: str
    promptBorderShimmer: str
    text: str
    inverseText: str
    inactive: str
    inactiveShimmer: str
    subtle: str
    suggestion: str
    remember: str
    background: str
    success: str
    error: str
    warning: str
    merged: str
    warningShimmer: str
    diffAdded: str
    diffRemoved: str
    diffAddedDimmed: str
    diffRemovedDimmed: str
    diffAddedWord: str
    diffRemovedWord: str
    red_FOR_SUBAGENTS_ONLY: str
    blue_FOR_SUBAGENTS_ONLY: str
    green_FOR_SUBAGENTS_ONLY: str
    yellow_FOR_SUBAGENTS_ONLY: str
    purple_FOR_SUBAGENTS_ONLY: str
    orange_FOR_SUBAGENTS_ONLY: str
    pink_FOR_SUBAGENTS_ONLY: str
    cyan_FOR_SUBAGENTS_ONLY: str
    professionalBlue: str
    chromeYellow: str
    clawd_body: str
    clawd_background: str
    userMessageBackground: str
    userMessageBackgroundHover: str
    messageActionsBackground: str
    selectionBg: str
    bashMessageBackgroundColor: str
    memoryBackgroundColor: str
    rate_limit_fill: str
    rate_limit_empty: str
    briefLabelYou: str
    briefLabelTabvis: str
    rainbow_red: str
    rainbow_orange: str
    rainbow_yellow: str
    rainbow_green: str
    rainbow_blue: str
    rainbow_indigo: str
    rainbow_violet: str
    rainbow_red_shimmer: str
    rainbow_orange_shimmer: str
    rainbow_yellow_shimmer: str
    rainbow_green_shimmer: str
    rainbow_blue_shimmer: str
    rainbow_indigo_shimmer: str
    rainbow_violet_shimmer: str


THEME_NAMES = (
    "dark",
    "light",
    "light-daltonized",
    "dark-daltonized",
    "light-ansi",
    "dark-ansi",
)

ThemeName = Literal[
    "dark",
    "light",
    "light-daltonized",
    "dark-daltonized",
    "light-ansi",
    "dark-ansi",
]

THEME_SETTINGS = ("auto", *THEME_NAMES)

ThemeSetting = Literal[
    "auto",
    "dark",
    "light",
    "light-daltonized",
    "dark-daltonized",
    "light-ansi",
    "dark-ansi",
]


# Light theme using explicit RGB values to avoid inconsistencies from users' custom terminal ANSI
# color definitions.
_LIGHT_THEME: Theme = {
    "autoAccept": "rgb(135,0,255)",
    "bashBorder": "rgb(255,0,135)",
    "tabvis": "rgb(215,119,87)",
    "tabvisShimmer": "rgb(245,149,117)",
    "systemBlue_FOR_SYSTEM_SPINNER": "rgb(87,105,247)",
    "systemBlueShimmer_FOR_SYSTEM_SPINNER": "rgb(117,135,255)",
    "permission": "rgb(87,105,247)",
    "permissionShimmer": "rgb(137,155,255)",
    "planMode": "rgb(0,102,102)",
    "ide": "rgb(71,130,200)",
    "promptBorder": "rgb(153,153,153)",
    "promptBorderShimmer": "rgb(183,183,183)",
    "text": "rgb(0,0,0)",
    "inverseText": "rgb(255,255,255)",
    "inactive": "rgb(102,102,102)",
    "inactiveShimmer": "rgb(142,142,142)",
    "subtle": "rgb(175,175,175)",
    "suggestion": "rgb(87,105,247)",
    "remember": "rgb(0,0,255)",
    "background": "rgb(0,153,153)",
    "success": "rgb(44,122,57)",
    "error": "rgb(171,43,63)",
    "warning": "rgb(150,108,30)",
    "merged": "rgb(135,0,255)",
    "warningShimmer": "rgb(200,158,80)",
    "diffAdded": "rgb(105,219,124)",
    "diffRemoved": "rgb(255,168,180)",
    "diffAddedDimmed": "rgb(199,225,203)",
    "diffRemovedDimmed": "rgb(253,210,216)",
    "diffAddedWord": "rgb(47,157,68)",
    "diffRemovedWord": "rgb(209,69,75)",
    "red_FOR_SUBAGENTS_ONLY": "rgb(220,38,38)",
    "blue_FOR_SUBAGENTS_ONLY": "rgb(37,99,235)",
    "green_FOR_SUBAGENTS_ONLY": "rgb(22,163,74)",
    "yellow_FOR_SUBAGENTS_ONLY": "rgb(202,138,4)",
    "purple_FOR_SUBAGENTS_ONLY": "rgb(147,51,234)",
    "orange_FOR_SUBAGENTS_ONLY": "rgb(234,88,12)",
    "pink_FOR_SUBAGENTS_ONLY": "rgb(219,39,119)",
    "cyan_FOR_SUBAGENTS_ONLY": "rgb(8,145,178)",
    "professionalBlue": "rgb(106,155,204)",
    "chromeYellow": "rgb(251,188,4)",
    "clawd_body": "rgb(215,119,87)",
    "clawd_background": "rgb(0,0,0)",
    "userMessageBackground": "rgb(240, 240, 240)",
    "userMessageBackgroundHover": "rgb(252, 252, 252)",
    "messageActionsBackground": "rgb(232, 236, 244)",
    "selectionBg": "rgb(180, 213, 255)",
    "bashMessageBackgroundColor": "rgb(250, 245, 250)",
    "memoryBackgroundColor": "rgb(230, 245, 250)",
    "rate_limit_fill": "rgb(87,105,247)",
    "rate_limit_empty": "rgb(39,47,111)",
    "briefLabelYou": "rgb(37,99,235)",
    "briefLabelTabvis": "rgb(215,119,87)",
    "rainbow_red": "rgb(235,95,87)",
    "rainbow_orange": "rgb(245,139,87)",
    "rainbow_yellow": "rgb(250,195,95)",
    "rainbow_green": "rgb(145,200,130)",
    "rainbow_blue": "rgb(130,170,220)",
    "rainbow_indigo": "rgb(155,130,200)",
    "rainbow_violet": "rgb(200,130,180)",
    "rainbow_red_shimmer": "rgb(250,155,147)",
    "rainbow_orange_shimmer": "rgb(255,185,137)",
    "rainbow_yellow_shimmer": "rgb(255,225,155)",
    "rainbow_green_shimmer": "rgb(185,230,180)",
    "rainbow_blue_shimmer": "rgb(180,205,240)",
    "rainbow_indigo_shimmer": "rgb(195,180,230)",
    "rainbow_violet_shimmer": "rgb(230,180,210)",
}


# Light ANSI theme using only the 16 standard ANSI colors for terminals without true color.
_LIGHT_ANSI_THEME: Theme = {
    "autoAccept": "ansi:magenta",
    "bashBorder": "ansi:magenta",
    "tabvis": "ansi:redBright",
    "tabvisShimmer": "ansi:yellowBright",
    "systemBlue_FOR_SYSTEM_SPINNER": "ansi:blue",
    "systemBlueShimmer_FOR_SYSTEM_SPINNER": "ansi:blueBright",
    "permission": "ansi:blue",
    "permissionShimmer": "ansi:blueBright",
    "planMode": "ansi:cyan",
    "ide": "ansi:blueBright",
    "promptBorder": "ansi:white",
    "promptBorderShimmer": "ansi:whiteBright",
    "text": "ansi:black",
    "inverseText": "ansi:white",
    "inactive": "ansi:blackBright",
    "inactiveShimmer": "ansi:white",
    "subtle": "ansi:blackBright",
    "suggestion": "ansi:blue",
    "remember": "ansi:blue",
    "background": "ansi:cyan",
    "success": "ansi:green",
    "error": "ansi:red",
    "warning": "ansi:yellow",
    "merged": "ansi:magenta",
    "warningShimmer": "ansi:yellowBright",
    "diffAdded": "ansi:green",
    "diffRemoved": "ansi:red",
    "diffAddedDimmed": "ansi:green",
    "diffRemovedDimmed": "ansi:red",
    "diffAddedWord": "ansi:greenBright",
    "diffRemovedWord": "ansi:redBright",
    "red_FOR_SUBAGENTS_ONLY": "ansi:red",
    "blue_FOR_SUBAGENTS_ONLY": "ansi:blue",
    "green_FOR_SUBAGENTS_ONLY": "ansi:green",
    "yellow_FOR_SUBAGENTS_ONLY": "ansi:yellow",
    "purple_FOR_SUBAGENTS_ONLY": "ansi:magenta",
    "orange_FOR_SUBAGENTS_ONLY": "ansi:redBright",
    "pink_FOR_SUBAGENTS_ONLY": "ansi:magentaBright",
    "cyan_FOR_SUBAGENTS_ONLY": "ansi:cyan",
    "professionalBlue": "ansi:blueBright",
    "chromeYellow": "ansi:yellow",
    "clawd_body": "ansi:redBright",
    "clawd_background": "ansi:black",
    "userMessageBackground": "ansi:white",
    "userMessageBackgroundHover": "ansi:whiteBright",
    "messageActionsBackground": "ansi:white",
    "selectionBg": "ansi:cyan",
    "bashMessageBackgroundColor": "ansi:whiteBright",
    "memoryBackgroundColor": "ansi:white",
    "rate_limit_fill": "ansi:yellow",
    "rate_limit_empty": "ansi:black",
    "briefLabelYou": "ansi:blue",
    "briefLabelTabvis": "ansi:redBright",
    "rainbow_red": "ansi:red",
    "rainbow_orange": "ansi:redBright",
    "rainbow_yellow": "ansi:yellow",
    "rainbow_green": "ansi:green",
    "rainbow_blue": "ansi:cyan",
    "rainbow_indigo": "ansi:blue",
    "rainbow_violet": "ansi:magenta",
    "rainbow_red_shimmer": "ansi:redBright",
    "rainbow_orange_shimmer": "ansi:yellow",
    "rainbow_yellow_shimmer": "ansi:yellowBright",
    "rainbow_green_shimmer": "ansi:greenBright",
    "rainbow_blue_shimmer": "ansi:cyanBright",
    "rainbow_indigo_shimmer": "ansi:blueBright",
    "rainbow_violet_shimmer": "ansi:magentaBright",
}


# Dark ANSI theme using only the 16 standard ANSI colors for terminals without true color.
_DARK_ANSI_THEME: Theme = {
    "autoAccept": "ansi:magentaBright",
    "bashBorder": "ansi:magentaBright",
    "tabvis": "ansi:redBright",
    "tabvisShimmer": "ansi:yellowBright",
    "systemBlue_FOR_SYSTEM_SPINNER": "ansi:blueBright",
    "systemBlueShimmer_FOR_SYSTEM_SPINNER": "ansi:blueBright",
    "permission": "ansi:blueBright",
    "permissionShimmer": "ansi:blueBright",
    "planMode": "ansi:cyanBright",
    "ide": "ansi:blue",
    "promptBorder": "ansi:white",
    "promptBorderShimmer": "ansi:whiteBright",
    "text": "ansi:whiteBright",
    "inverseText": "ansi:black",
    "inactive": "ansi:white",
    "inactiveShimmer": "ansi:whiteBright",
    "subtle": "ansi:white",
    "suggestion": "ansi:blueBright",
    "remember": "ansi:blueBright",
    "background": "ansi:cyanBright",
    "success": "ansi:greenBright",
    "error": "ansi:redBright",
    "warning": "ansi:yellowBright",
    "merged": "ansi:magentaBright",
    "warningShimmer": "ansi:yellowBright",
    "diffAdded": "ansi:green",
    "diffRemoved": "ansi:red",
    "diffAddedDimmed": "ansi:green",
    "diffRemovedDimmed": "ansi:red",
    "diffAddedWord": "ansi:greenBright",
    "diffRemovedWord": "ansi:redBright",
    "red_FOR_SUBAGENTS_ONLY": "ansi:redBright",
    "blue_FOR_SUBAGENTS_ONLY": "ansi:blueBright",
    "green_FOR_SUBAGENTS_ONLY": "ansi:greenBright",
    "yellow_FOR_SUBAGENTS_ONLY": "ansi:yellowBright",
    "purple_FOR_SUBAGENTS_ONLY": "ansi:magentaBright",
    "orange_FOR_SUBAGENTS_ONLY": "ansi:redBright",
    "pink_FOR_SUBAGENTS_ONLY": "ansi:magentaBright",
    "cyan_FOR_SUBAGENTS_ONLY": "ansi:cyanBright",
    "professionalBlue": "rgb(106,155,204)",
    "chromeYellow": "ansi:yellowBright",
    "clawd_body": "ansi:redBright",
    "clawd_background": "ansi:black",
    "userMessageBackground": "ansi:blackBright",
    "userMessageBackgroundHover": "ansi:white",
    "messageActionsBackground": "ansi:blackBright",
    "selectionBg": "ansi:blue",
    "bashMessageBackgroundColor": "ansi:black",
    "memoryBackgroundColor": "ansi:blackBright",
    "rate_limit_fill": "ansi:yellow",
    "rate_limit_empty": "ansi:white",
    "briefLabelYou": "ansi:blueBright",
    "briefLabelTabvis": "ansi:redBright",
    "rainbow_red": "ansi:red",
    "rainbow_orange": "ansi:redBright",
    "rainbow_yellow": "ansi:yellow",
    "rainbow_green": "ansi:green",
    "rainbow_blue": "ansi:cyan",
    "rainbow_indigo": "ansi:blue",
    "rainbow_violet": "ansi:magenta",
    "rainbow_red_shimmer": "ansi:redBright",
    "rainbow_orange_shimmer": "ansi:yellow",
    "rainbow_yellow_shimmer": "ansi:yellowBright",
    "rainbow_green_shimmer": "ansi:greenBright",
    "rainbow_blue_shimmer": "ansi:cyanBright",
    "rainbow_indigo_shimmer": "ansi:blueBright",
    "rainbow_violet_shimmer": "ansi:magentaBright",
}


# Light daltonized theme (color-blind friendly) using explicit RGB values.
_LIGHT_DALTONIZED_THEME: Theme = {
    "autoAccept": "rgb(135,0,255)",
    "bashBorder": "rgb(0,102,204)",
    "tabvis": "rgb(255,153,51)",
    "tabvisShimmer": "rgb(255,183,101)",
    "systemBlue_FOR_SYSTEM_SPINNER": "rgb(51,102,255)",
    "systemBlueShimmer_FOR_SYSTEM_SPINNER": "rgb(101,152,255)",
    "permission": "rgb(51,102,255)",
    "permissionShimmer": "rgb(101,152,255)",
    "planMode": "rgb(51,102,102)",
    "ide": "rgb(71,130,200)",
    "promptBorder": "rgb(153,153,153)",
    "promptBorderShimmer": "rgb(183,183,183)",
    "text": "rgb(0,0,0)",
    "inverseText": "rgb(255,255,255)",
    "inactive": "rgb(102,102,102)",
    "inactiveShimmer": "rgb(142,142,142)",
    "subtle": "rgb(175,175,175)",
    "suggestion": "rgb(51,102,255)",
    "remember": "rgb(51,102,255)",
    "background": "rgb(0,153,153)",
    "success": "rgb(0,102,153)",
    "error": "rgb(204,0,0)",
    "warning": "rgb(255,153,0)",
    "merged": "rgb(135,0,255)",
    "warningShimmer": "rgb(255,183,50)",
    "diffAdded": "rgb(153,204,255)",
    "diffRemoved": "rgb(255,204,204)",
    "diffAddedDimmed": "rgb(209,231,253)",
    "diffRemovedDimmed": "rgb(255,233,233)",
    "diffAddedWord": "rgb(51,102,204)",
    "diffRemovedWord": "rgb(153,51,51)",
    "red_FOR_SUBAGENTS_ONLY": "rgb(204,0,0)",
    "blue_FOR_SUBAGENTS_ONLY": "rgb(0,102,204)",
    "green_FOR_SUBAGENTS_ONLY": "rgb(0,204,0)",
    "yellow_FOR_SUBAGENTS_ONLY": "rgb(255,204,0)",
    "purple_FOR_SUBAGENTS_ONLY": "rgb(128,0,128)",
    "orange_FOR_SUBAGENTS_ONLY": "rgb(255,128,0)",
    "pink_FOR_SUBAGENTS_ONLY": "rgb(255,102,178)",
    "cyan_FOR_SUBAGENTS_ONLY": "rgb(0,178,178)",
    "professionalBlue": "rgb(106,155,204)",
    "chromeYellow": "rgb(251,188,4)",
    "clawd_body": "rgb(215,119,87)",
    "clawd_background": "rgb(0,0,0)",
    "userMessageBackground": "rgb(220, 220, 220)",
    "userMessageBackgroundHover": "rgb(232, 232, 232)",
    "messageActionsBackground": "rgb(210, 216, 226)",
    "selectionBg": "rgb(180, 213, 255)",
    "bashMessageBackgroundColor": "rgb(250, 245, 250)",
    "memoryBackgroundColor": "rgb(230, 245, 250)",
    "rate_limit_fill": "rgb(51,102,255)",
    "rate_limit_empty": "rgb(23,46,114)",
    "briefLabelYou": "rgb(37,99,235)",
    "briefLabelTabvis": "rgb(255,153,51)",
    "rainbow_red": "rgb(235,95,87)",
    "rainbow_orange": "rgb(245,139,87)",
    "rainbow_yellow": "rgb(250,195,95)",
    "rainbow_green": "rgb(145,200,130)",
    "rainbow_blue": "rgb(130,170,220)",
    "rainbow_indigo": "rgb(155,130,200)",
    "rainbow_violet": "rgb(200,130,180)",
    "rainbow_red_shimmer": "rgb(250,155,147)",
    "rainbow_orange_shimmer": "rgb(255,185,137)",
    "rainbow_yellow_shimmer": "rgb(255,225,155)",
    "rainbow_green_shimmer": "rgb(185,230,180)",
    "rainbow_blue_shimmer": "rgb(180,205,240)",
    "rainbow_indigo_shimmer": "rgb(195,180,230)",
    "rainbow_violet_shimmer": "rgb(230,180,210)",
}


# Dark theme using explicit RGB values.
_DARK_THEME: Theme = {
    "autoAccept": "rgb(175,135,255)",
    "bashBorder": "rgb(253,93,177)",
    "tabvis": "rgb(215,119,87)",
    "tabvisShimmer": "rgb(235,159,127)",
    "systemBlue_FOR_SYSTEM_SPINNER": "rgb(147,165,255)",
    "systemBlueShimmer_FOR_SYSTEM_SPINNER": "rgb(177,195,255)",
    "permission": "rgb(177,185,249)",
    "permissionShimmer": "rgb(207,215,255)",
    "planMode": "rgb(72,150,140)",
    "ide": "rgb(71,130,200)",
    "promptBorder": "rgb(136,136,136)",
    "promptBorderShimmer": "rgb(166,166,166)",
    "text": "rgb(255,255,255)",
    "inverseText": "rgb(0,0,0)",
    "inactive": "rgb(153,153,153)",
    "inactiveShimmer": "rgb(193,193,193)",
    "subtle": "rgb(80,80,80)",
    "suggestion": "rgb(177,185,249)",
    "remember": "rgb(177,185,249)",
    "background": "rgb(0,204,204)",
    "success": "rgb(78,186,101)",
    "error": "rgb(255,107,128)",
    "warning": "rgb(255,193,7)",
    "merged": "rgb(175,135,255)",
    "warningShimmer": "rgb(255,223,57)",
    "diffAdded": "rgb(34,92,43)",
    "diffRemoved": "rgb(122,41,54)",
    "diffAddedDimmed": "rgb(71,88,74)",
    "diffRemovedDimmed": "rgb(105,72,77)",
    "diffAddedWord": "rgb(56,166,96)",
    "diffRemovedWord": "rgb(179,89,107)",
    "red_FOR_SUBAGENTS_ONLY": "rgb(220,38,38)",
    "blue_FOR_SUBAGENTS_ONLY": "rgb(37,99,235)",
    "green_FOR_SUBAGENTS_ONLY": "rgb(22,163,74)",
    "yellow_FOR_SUBAGENTS_ONLY": "rgb(202,138,4)",
    "purple_FOR_SUBAGENTS_ONLY": "rgb(147,51,234)",
    "orange_FOR_SUBAGENTS_ONLY": "rgb(234,88,12)",
    "pink_FOR_SUBAGENTS_ONLY": "rgb(219,39,119)",
    "cyan_FOR_SUBAGENTS_ONLY": "rgb(8,145,178)",
    "professionalBlue": "rgb(106,155,204)",
    "chromeYellow": "rgb(251,188,4)",
    "clawd_body": "rgb(215,119,87)",
    "clawd_background": "rgb(0,0,0)",
    "userMessageBackground": "rgb(55, 55, 55)",
    "userMessageBackgroundHover": "rgb(70, 70, 70)",
    "messageActionsBackground": "rgb(44, 50, 62)",
    "selectionBg": "rgb(38, 79, 120)",
    "bashMessageBackgroundColor": "rgb(65, 60, 65)",
    "memoryBackgroundColor": "rgb(55, 65, 70)",
    "rate_limit_fill": "rgb(177,185,249)",
    "rate_limit_empty": "rgb(80,83,112)",
    "briefLabelYou": "rgb(122,180,232)",
    "briefLabelTabvis": "rgb(215,119,87)",
    "rainbow_red": "rgb(235,95,87)",
    "rainbow_orange": "rgb(245,139,87)",
    "rainbow_yellow": "rgb(250,195,95)",
    "rainbow_green": "rgb(145,200,130)",
    "rainbow_blue": "rgb(130,170,220)",
    "rainbow_indigo": "rgb(155,130,200)",
    "rainbow_violet": "rgb(200,130,180)",
    "rainbow_red_shimmer": "rgb(250,155,147)",
    "rainbow_orange_shimmer": "rgb(255,185,137)",
    "rainbow_yellow_shimmer": "rgb(255,225,155)",
    "rainbow_green_shimmer": "rgb(185,230,180)",
    "rainbow_blue_shimmer": "rgb(180,205,240)",
    "rainbow_indigo_shimmer": "rgb(195,180,230)",
    "rainbow_violet_shimmer": "rgb(230,180,210)",
}


# Dark daltonized theme (color-blind friendly) using explicit RGB values.
_DARK_DALTONIZED_THEME: Theme = {
    "autoAccept": "rgb(175,135,255)",
    "bashBorder": "rgb(51,153,255)",
    "tabvis": "rgb(255,153,51)",
    "tabvisShimmer": "rgb(255,183,101)",
    "systemBlue_FOR_SYSTEM_SPINNER": "rgb(153,204,255)",
    "systemBlueShimmer_FOR_SYSTEM_SPINNER": "rgb(183,224,255)",
    "permission": "rgb(153,204,255)",
    "permissionShimmer": "rgb(183,224,255)",
    "planMode": "rgb(102,153,153)",
    "ide": "rgb(71,130,200)",
    "promptBorder": "rgb(136,136,136)",
    "promptBorderShimmer": "rgb(166,166,166)",
    "text": "rgb(255,255,255)",
    "inverseText": "rgb(0,0,0)",
    "inactive": "rgb(153,153,153)",
    "inactiveShimmer": "rgb(193,193,193)",
    "subtle": "rgb(80,80,80)",
    "suggestion": "rgb(153,204,255)",
    "remember": "rgb(153,204,255)",
    "background": "rgb(0,204,204)",
    "success": "rgb(51,153,255)",
    "error": "rgb(255,102,102)",
    "warning": "rgb(255,204,0)",
    "merged": "rgb(175,135,255)",
    "warningShimmer": "rgb(255,234,50)",
    "diffAdded": "rgb(0,68,102)",
    "diffRemoved": "rgb(102,0,0)",
    "diffAddedDimmed": "rgb(62,81,91)",
    "diffRemovedDimmed": "rgb(62,44,44)",
    "diffAddedWord": "rgb(0,119,179)",
    "diffRemovedWord": "rgb(179,0,0)",
    "red_FOR_SUBAGENTS_ONLY": "rgb(255,102,102)",
    "blue_FOR_SUBAGENTS_ONLY": "rgb(102,178,255)",
    "green_FOR_SUBAGENTS_ONLY": "rgb(102,255,102)",
    "yellow_FOR_SUBAGENTS_ONLY": "rgb(255,255,102)",
    "purple_FOR_SUBAGENTS_ONLY": "rgb(178,102,255)",
    "orange_FOR_SUBAGENTS_ONLY": "rgb(255,178,102)",
    "pink_FOR_SUBAGENTS_ONLY": "rgb(255,153,204)",
    "cyan_FOR_SUBAGENTS_ONLY": "rgb(102,204,204)",
    "professionalBlue": "rgb(106,155,204)",
    "chromeYellow": "rgb(251,188,4)",
    "clawd_body": "rgb(215,119,87)",
    "clawd_background": "rgb(0,0,0)",
    "userMessageBackground": "rgb(55, 55, 55)",
    "userMessageBackgroundHover": "rgb(70, 70, 70)",
    "messageActionsBackground": "rgb(44, 50, 62)",
    "selectionBg": "rgb(38, 79, 120)",
    "bashMessageBackgroundColor": "rgb(65, 60, 65)",
    "memoryBackgroundColor": "rgb(55, 65, 70)",
    "rate_limit_fill": "rgb(153,204,255)",
    "rate_limit_empty": "rgb(69,92,115)",
    "briefLabelYou": "rgb(122,180,232)",
    "briefLabelTabvis": "rgb(255,153,51)",
    "rainbow_red": "rgb(235,95,87)",
    "rainbow_orange": "rgb(245,139,87)",
    "rainbow_yellow": "rgb(250,195,95)",
    "rainbow_green": "rgb(145,200,130)",
    "rainbow_blue": "rgb(130,170,220)",
    "rainbow_indigo": "rgb(155,130,200)",
    "rainbow_violet": "rgb(200,130,180)",
    "rainbow_red_shimmer": "rgb(250,155,147)",
    "rainbow_orange_shimmer": "rgb(255,185,137)",
    "rainbow_yellow_shimmer": "rgb(255,225,155)",
    "rainbow_green_shimmer": "rgb(185,230,180)",
    "rainbow_blue_shimmer": "rgb(180,205,240)",
    "rainbow_indigo_shimmer": "rgb(195,180,230)",
    "rainbow_violet_shimmer": "rgb(230,180,210)",
}


def get_theme(theme_name: ThemeName) -> Theme:
    """Resolve a :data:`ThemeName` to its concrete :data:`Theme` palette (default: dark)."""
    if theme_name == "light":
        return _LIGHT_THEME
    if theme_name == "light-ansi":
        return _LIGHT_ANSI_THEME
    if theme_name == "dark-ansi":
        return _DARK_ANSI_THEME
    if theme_name == "light-daltonized":
        return _LIGHT_DALTONIZED_THEME
    if theme_name == "dark-daltonized":
        return _DARK_DALTONIZED_THEME
    return _DARK_THEME


_RGB_RE = re.compile(r"rgb\(\s?(\d+),\s?(\d+),\s?(\d+)\s?\)")


def _rgb_to_ansi256(r: int, g: int, b: int) -> int:
    """Convert an RGB triple to the nearest xterm-256 color index.

    Mirrors chalk/ansi-styles' ``rgbToAnsi256``: grayscale ramp for near-equal channels, else the
    6×6×6 color cube. Used for Apple Terminal's 256-color (level 2) mode.
    """
    if r == g == b:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return round(((r - 8) / 247) * 24) + 232
    return (
        16
        + 36 * round(r / 255 * 5)
        + 6 * round(g / 255 * 5)
        + round(b / 255 * 5)
    )


def _is_apple_terminal() -> bool:
    # Apple Terminal doesn't handle 24-bit color escape sequences well → use 256-color mode.
    return env.terminal == "Apple_Terminal"


def theme_color_to_ansi(theme_color: str) -> str:
    """Convert a theme color to an ANSI escape sequence for use with asciichart.

    Truecolor by default (``\\x1b[38;2;r;g;bm``); 256-color (``\\x1b[38;5;Nm``) for Apple Terminal.
    Falls back to magenta (``\\x1b[35m``) if the color is not an ``rgb(...)`` string.
    """
    rgb_match = _RGB_RE.search(theme_color)
    if rgb_match:
        r = int(rgb_match.group(1))
        g = int(rgb_match.group(2))
        b = int(rgb_match.group(3))
        if _is_apple_terminal():
            return f"\x1b[38;5;{_rgb_to_ansi256(r, g, b)}m"
        return f"\x1b[38;2;{r};{g};{b}m"
    # Fallback to magenta if parsing fails.
    return "\x1b[35m"
