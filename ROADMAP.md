# What's verified, and what's next

## What's verified

SITREP91 only starts a run it's been verified for.

| Chemistry  | Charge       | Storage      | Discharge    | Cycle        | Refresh      | Break-in     |
|------------|--------------|--------------|--------------|--------------|--------------|--------------|
| Li-ion     | verified     | verified     | verified     | monitor only | n/a          | n/a          |
| Li-ion HV  | monitor only | monitor only | monitor only | monitor only | n/a          | n/a          |
| LiFePO4    | monitor only | monitor only | monitor only | monitor only | n/a          | n/a          |
| LTO        | monitor only | monitor only | monitor only | monitor only | n/a          | n/a          |
| Na-ion     | monitor only | monitor only | monitor only | monitor only | n/a          | n/a          |
| NiMH       | verified     | n/a          | verified     | monitor only | monitor only | monitor only |
| Eneloop    | verified     | n/a          | verified     | monitor only | monitor only | monitor only |
| NiCd       | monitor only | n/a          | monitor only | monitor only | monitor only | monitor only |
| NiZn       | monitor only | monitor only | n/a          | monitor only | n/a          | n/a          |
| RAM        | monitor only | monitor only | n/a          | monitor only | n/a          | n/a          |

**Verified** means I've run it on my own charger, and SITREP91 can start and
stop it.

**Monitor only** means SITREP91 watches and logs it when it's started at the
charger's panel, but won't start it. That isn't a warning about the
combination. I just haven't run it myself yet.

**n/a** means the charger's panel doesn't offer that mode for that chemistry.

A few notes on the table:

- Cycle, Refresh and Break-in are monitor only for every chemistry, because
  SITREP91 has no start settings for them yet.
- NiMH, NiCd and Eneloop have no storage mode.
- NiZn and RAM stay monitor only until I've checked their modes on hardware.
- Of the monitor-only modes, I've only watched a Break-in. Cycle and Refresh
  should show up the same way, but a Cycle has several phases, and it may land
  in more than one run folder.

## What's next

No dates. This is one person's spare time, and the order may change.

1. **LiFePO4, LTO and Na-ion control.** Test cells are already ordered.
2. **Saved setups.** Set a slot up once and use the same settings again later.
3. **More charge settings.** Some of the panel's settings aren't in Configure
   yet. Nickel keep voltage can only be set at the panel, because the charger
   ignores it over Bluetooth.
4. **Show what a run is doing** (charge, discharge or storage) for runs started
   at the charger's panel or in SkyRC's app.
5. **Cards that rearrange themselves.** On narrower screens, lay the four slot
   cards out two by two instead of in a row, so the window never needs to be
   wider than the screen.
6. **Names for the remaining alert codes.** Anything that shows as "alert code
   N" today. A report with what the charger's panel said is what gets one
   named.
7. **Cycle, Refresh and Break-in, and storage for the other lithium
   chemistries.** SITREP91 watches whatever the charger runs, though so
   far I've only watched a Break-in.
8. **A catalog of cell models with published specs**, so a program can be
   filled in from the cell you pick.

**Li-ion HV, NiCd, NiZn and RAM** are on the list only if people ask for them.
If you want one, say so. That's what moves it up.
