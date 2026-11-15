# Planet X and SPHEREx — Challenge Details

- **Event:** 2026 NASA Space Apps Challenge
- **Difficulty:** Advanced
- **Subjects:** Astrophysics, Planets & Moons, Software
- **Challenge URL:** https://www.spaceappschallenge.org/2026/challenges/planet-x-and-spherex/

## Summary

Since 2025, NASA's SPHEREx mission has been mapping the entire sky every six months in 102 bands of near-infrared light, returning images of more than a billion objects. Comets, asteroids, stars, brown dwarfs, and even new planets reveal themselves by shifting position between images, but no one person can sift through all that data alone. Your challenge is to create a public-facing web tool to display images of the sky from the SPHEREx mission, making it easy for anyone to quickly see how they change over time.

## Background

Asteroids and comets orbit around the Sun. Nearby stars march across the sky as they orbit around the Galaxy. The Earth orbits the Sun too, causing objects near us to change their apparent positions. Watching for these motions helps us discover new astronomical objects and also helps measure the distances to these objects. For example, the dwarf planet Pluto was discovered when an astronomer compared two images of the sky taken at different times and recognized that one point of light had shifted between images.

More recently, astronomers have mounted massive searches for nearby objects using this process with help from the public. Hundreds of thousands of volunteers have used this technique to discover asteroids, comets, stars, and brown dwarfs—balls of gas too massive to be considered planets yet without enough mass to burn like stars. In fact, most of the known comets and brown dwarfs were discovered by volunteers in this manner through projects like [Backyard Worlds](https://www.backyardworlds.org).

Since May 2025, NASA's new SPHEREx mission has been returning unique data that could yield new discoveries. SPHEREx is mapping the whole sky once every six months in 102 bands of near-infrared light, making it uniquely sensitive to objects that are rich in hydrogen, water, carbon dioxide, carbon monoxide, and many other molecules. There will be too much data for any one person or even computer code to completely comprehend. There will be images of more than a billion objects—including brown dwarfs, quasars, comets, asteroids, stars, supernovae, and galaxies—which often overlap or vary in brightness. We need your creative new tools to make the most of this treasure trove and help as many people as possible pitch in to make discoveries.

## Objectives

1. **Build a Public-Facing Web Tool:** Create an accessible web application to display sky images from NASA's SPHEREx mission so anyone can quickly observe how objects change over time.
2. **Coordinate-Based Time-Series Viewer:** Allow users to enter the coordinates of a patch of sky (**Right Ascension** and **Declination**) and display a series of images of that patch like a short video/animation ("blinking") to make moving or variable objects easy to spot.
3. **Validate on Benchmark Targets:** Test your tool on the following known high-proper-motion and variable objects:
   - **Barnard's Star** (high-proper-motion red dwarf)
   - **Epsilon Indi B** (a pair of brown dwarfs orbiting a star)
   - **WISE 0855** (a fast-moving brown dwarf located at Right Ascension `08h55m10.831s`, Declination `-7d14m42.53s`)
   - **RW Aurigae** (variable star)
   - **R Draconis** (variable star)

## Potential Considerations

You may (but are not required to) incorporate the following features:

- **Continuous Data Ingestion:** New SPHEREx data arrives rapidly; design the tool so it is easy to update with the latest data releases.
- **102 Near-Infrared Bands & Detector Arrays:** Think creatively about visualizing the 102 infrared bands—for example, separately co-adding (stacking) data taken by each of the **six detector arrays** to improve the signal-to-noise ratio (SNR) in displayed images. Extra kudos if your tool can generate spectra on the fly.
- **Image Stacking & Differencing:** Provide options to add together (co-add) or subtract images taken at different epochs to highlight moving or transient sources.
- **Adjustable Field of View (FOV):** Let users choose the field of view—larger fields of view help track faster-moving sources, balanced against longer data transfer times.
- **Multi-Mission Data Overlays:** Allow users to overlay astronomical catalog or imaging data from complementary surveys, such as the **Gaia** mission or the **WISE** mission.
- **Shareable & Embeddable URLs:** Support querying the tool via coordinates in the URL parameters so users can easily share links or embed views in other websites and projects.
- **Metadata & Quality Flags:** Display relevant astronomical metadata, such as contamination flags.
- **Existing Astronomy Libraries & Viewers:** Leverage open-source astronomical data handling and visualization tools such as **Astropy** and **Aladin** (developed by the Strasbourg Astronomical Data Center, CDS).
