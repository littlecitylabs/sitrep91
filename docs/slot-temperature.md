# The epic tale of four slot temperatures

The MC5000 has a temperature sensor in each of its four bays, and it will
report the reading over Bluetooth if you ask. SkyRC's app never asks, and I
haven't found it in the manual. This is how I found it, and how I checked that
it means something.

## How it was found

The charger clearly knew its own temperature. Its panel has an
over-temperature error, and its Bluetooth configuration has a
temperature-cutoff field. So the sensors had to be in there somewhere, and I
went looking. Digging through the charger's firmware for how it controls its
fan turned up seven internal temperature channels. From there I traced the
request that reads the slots, `0x55`, and confirmed it on the hardware the
same day. I haven't yet traced where the other three sit, or looked for a way to read
them.

## Why the existing projects missed it

Two public reverse-engineering projects looked at temperature on the MC5000,
and neither found it. Both were working from captures of the
app's Bluetooth traffic. The charger answers `0x55`, but in the captures I've
seen its own app never sends it, so there was nothing to watch. Reading the firmware got around that.

## How I know it's real

It's a real per-slot measurement, and the cell in the slot is what drives it.
That's been checked, not assumed:

- **Each slot reads its own sensor.** A cell discharging in slot 2 pushed that
  slot from 26 to 35 °C while the other bays were empty. A sensor that
  ignored which slot you asked about would have read flat.
- **It responds to the cell, not to heat building up in the case.** During a
  charge's constant-voltage taper, the reading *fell* from 38 °C to 33 as the
  current tapered away. That's hard to explain by heat soaking into the
  enclosure.
- **With one cell running, the four bays agree.** One cell moved through all
  four slots in turn, with the neighboring bays empty and the same 500 mA
  discharge each time: 37, 37, 37, 37 (slot 1 read 36 on a cold first try,
  and 37 when re-run warm).
- **It tracks an independent probe.** With a thermocouple taped to the
  outside of the cell, over 52 minutes the charger's reading rose 3 °C while
  the probe rose 3.6 °C and the room cooled 1.4 °C. It reads warmer than the
  probe, which the next section covers.

## How it lines up with the cell

I opened the charger and couldn't pinpoint where each slot's sensor sits. I
believe it's just under the surface, or possibly at the battery contacts.

The charger reports whole degrees only, so every reading here is a whole
number. About 3 % of temperature requests come back empty, so the readings
are best-effort.

In one run I had two thermocouples going, one taped to the outside of the
cell and one on the bay floor under it. At the start, the charger read about
2 °C above the cell and about 1 °C above the floor. Once warm, it read about
5 °C above the cell and about 3 °C above the floor, and it had leveled off by
the 27-minute reading. That's one run and three charger readings, so treat the
shape as rough.

The sensor reads warmer than both probes, so it's picking up some heat
besides the cell's, possibly from the charger's own electronics under the
bay.

## Why the middle bays read high

With all four slots loaded, the middle bays read about 1 °C higher than the
edges, most likely because a middle bay has a working slot on either side of
it, and an edge bay has one neighbor and open air on the other. That's the chassis, not the
sensors. Rotating four cells through the bays left the pattern with the
*slots* (no slot's average moved more than 0.24 °C) while every cell in them
had changed.

With all four running, the fan also cycles about every six and a half minutes
and swings every slot's reading by 2 to 3 °C. That's bigger than the 1 °C
position difference, so compare averages over several minutes, not single
readings.

## What it can tell you

Honestly, the data is newer than my understanding of it. One early result,
offered as a data point rather than a claim: a worn-out cell with five times
the internal resistance of its healthy neighbors ran **about 2 °C hotter than
a healthy cell in the same bay position**, and an infrared thermometer on the
cells agreed, showing a 1.7 to 2.2 °C difference. The worn cell was discharging at
1500 mA. The 20 W limit held the healthy ones to about 1340 mA. The extra
resistance dissipates around 200 mW more heat. In a fan-cooled bay that came
out as about 2 °C, which is less than I'd estimated. By my estimate, at a
gentler 500 mA the same cell would have produced about a fifth of a degree,
which the sensor can't see.

In that same run, the worn cell, sitting in an edge bay, read 1 °C below a
healthy cell in a middle bay. Scan the four numbers for the odd one out and
you'd clear the bad cell and suspect a good one. Compare cells in the same
position, or a slot against its own history.

So temperature isn't the best way to find a *worn* cell. The charger's own
internal-resistance check flagged that same cell at five times normal, in a
few seconds, before the run started. Slot temperature may be more useful for
watching a run's trend, and for runs you aren't in the room for. I don't yet
know what it catches beyond a high-resistance cell.
