# Project Peek Design Reference

## Brand palette

| Token | Hex | Use |
| --- | --- | --- |
| School yellow | `#FFDD00` | Primary actions, active states, links, and focus indicators |
| Light gray | `#DBDDDF` | Light-mode canvas, borders, and dark-mode secondary text |
| Gray | `#3F4444` | Secondary text, borders, and elevated dark surfaces |
| Dark gray | `#222223` | Primary light-mode text and dark-mode surfaces |
| Black | `#000000` | Dark-mode canvas and text on yellow |
| White | `#FFFFFF` | Light-mode surfaces and dark-mode primary text |

The canonical CSS variables are defined at the top of `public/index.html` as
`--brand-yellow`, `--brand-gray-light`, `--brand-gray`, `--brand-gray-dark`,
`--brand-black`, and `--brand-white`.

Semantic status colors such as green, yellow, red, and gray remain separate from
the brand palette because they communicate sprint health and actions.
