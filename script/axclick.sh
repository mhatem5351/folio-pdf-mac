#!/usr/bin/env bash
# QA helper: click the first UI element of Folio PDF's front window whose
# accessibility name/description/title equals $1 (optionally role $2, e.g. AXButton).
osascript - "$1" "${2:-}" <<'APPLESCRIPT'
on run argv
  set target to item 1 of argv
  set wantRole to item 2 of argv
  tell application "System Events" to tell process "FolioPDF"
    repeat with el in (entire contents of window 1)
      try
        set r to role of el
        if wantRole is "" or r is wantRole then
          set n to ""
          set d to ""
          try
            set n to name of el
          end try
          try
            set d to description of el
          end try
          if n is target or d is target then
            click el
            return r & " " & target
          end if
        end if
      end try
    end repeat
  end tell
  return "not found: " & target
end run
APPLESCRIPT
