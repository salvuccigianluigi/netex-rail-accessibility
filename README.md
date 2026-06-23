# NeTEx Railway Accessibility

This repository contains a Python procedure for railway accessibility analysis using preprocessed NeTEx data.

The script computes, for each railway station used as origin, the set of reachable destination stations within a selected departure time window. The procedure supports direct connections and journeys with a configurable maximum number of changes.

## Main features

- Reads preprocessed NeTEx journey passage data from `journey_passages.csv`.
- Filters services according to the selected travel date.
- Uses `operating_periods.csv` and `day_type_assignments.csv` to identify valid `day_type_ref` values.
- Applies NeTEx boarding and alighting constraints through `for_boarding` and `for_alighting`.
- Converts timetable values into absolute minutes, including day offsets.
- Builds in-memory indexes of trips and departures by station.
- Performs a temporal breadth-first search from each origin station.
- Allows a maximum number of changes, typically up to two changes.
- Exports one CSV file per origin station with the best solution for each reachable destination.
- Optionally exports all alternative paths found.
- Produces a summary CSV file with the number of reachable stations by origin.

## Input data

The main input file is:

```text
journey_passages.csv
