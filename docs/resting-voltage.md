# Stale resting voltage, on the panel and over Bluetooth

When you put a cell in an idle bay, the MC5000 shows you its voltage. If you
leave it there, that number never changes again. The cell isn't holding
steady. The charger has stopped measuring it.

Everything here was measured on one charger running firmware 2.02.

**A resting slot shows the voltage from the last time that slot changed
state.** Inserting a cell is one of those moments. So is a run starting, and a
run stopping. In between, the reading is held indefinitely, however long you
leave it and however far the cell drifts.

In everyday use, **when a run ends, the panel settles on a voltage a couple of
minutes later and keeps showing it.** Come back four hours later and that's
still what it reads. A lithium cell rebounds tens of millivolts in the first
minutes after a discharge and keeps creeping upward for at least an hour, so
the number on screen is a snapshot from just after the run finished, whatever
the cell is sitting at now.

Nothing about it looks wrong. It's a plausible voltage, to three decimals, for
the cell that's actually in the slot. It's just old. SITREP91 labels it
"Volts · held" on a resting slot.

## How I know

- **A cell rested in a slot for 6 hours 54 minutes returned 2,486 consecutive
  readings of exactly 4.194 V**, without one digit of movement. The front
  panel showed the same value for the whole watch.
- **It isn't a Bluetooth artifact.** The panel and the protocol agree at every
  stage I compared, to the millivolt, because both are fed by the same measurement inside
  the charger. If one is stale, so is the other.
- **A cell that should have been moving still read flat.** One discharged at
  1500 mA for five minutes (125 mAh out), then reinserted and left strictly
  alone, gave **182 identical readings across half an hour**, while going by
  every other run it was still rebounding.
- **Forcing a measurement moves it.** The same cell, left an hour after a
  discharge, sat on the panel at **4.077 V**. I triggered a measurement
  without touching the charger, and the display went to **4.104 V**, then
  settled at 4.083 a few seconds after the stop. Same charger, same cell,
  nothing physically disturbed, and two different voltages seconds apart.
- **The display itself isn't frozen.** The charger recomputes the reported
  voltage constantly, from a value that only updates when the slot changes
  state, so it keeps faithfully recomputing the same stale number.

## What refreshes it

- putting a cell in, or taking one out
- starting a run
- stopping a run, plus, after a long run, two or three further updates over
  the next couple of minutes, after which it settles for good

After a long run you can watch that last one happen. After a discharge the
reading steps upward a few times about a minute apart, then stops. After a
charge it steps down instead. That's a handful of measurements that then
cease, and it isn't tracking the cell.

## What to do about it

**To get a current reading, lift the cell and set it back down.** That reads
true. Starting a run and stopping it right away also refreshes the
voltage, but it reads about 20 mV low, because the charger's resistance check
at the start pulls the cell down and it takes several minutes to recover,
nine in my test. I measured that with a run
started over Bluetooth, not the panel's Start and Stop buttons.

**If you're reading a number off a resting slot to decide something, check
when that slot last did anything.** Whether a cell held its charge, how two
cells compare, whether one is drooping: if the slot's last event was hours
ago, the number is from hours ago.

## What it does to a run's final voltage

A run's recorded final voltage isn't final. After a run ends, the cell's
voltage keeps relaxing toward its resting value: it rebounds upward after a
discharge and settles downward after a charge. A capture that exits when the
run ends writes down the value at that moment, and the charger re-measures it
a couple of times over the next few minutes with nothing listening. After one
short discharge the shift was about 30 mV. After a charge it went from 4.200
to 4.186 V on a run that ended at the 4.2 V ceiling. That's what the charger
reported at the time, so it isn't a recording error, but rested-voltage
figures taken from the last row of a capture read a little low after a
discharge and a little high after a charge.

If SITREP91 stays open, a finished slot keeps getting rows in its CSV file,
so the file does pick up the revised voltage. The run's recorded final
reading is taken at the end, though, and so is the reading of any capture
that exits when the run does.

Any tool built on the charger's standard status frame inherits this,
SITREP91 included. There's one voltage field, and it's the same one the panel
shows.

## Checking it yourself

Under an hour, no software. Discharge a cell for a few minutes and stop it.
Wait five minutes for the charger's last few updates to finish, then write
down what the panel says. Walk away for half an hour. It'll say exactly the
same thing. Now lift the cell out and put it back, and watch the number
jump.
