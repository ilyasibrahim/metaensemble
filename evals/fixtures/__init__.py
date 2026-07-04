"""Suite-A fixture source trees and the deterministic workspace builder.

`build.build_fixture` materializes a fixture source tree into a
single-commit git repository whose SHA is identical on every machine;
`build.FIXTURE_SHAS` records the expected SHAs that
`evals/datasets/suite_a/tasks.yaml` pins as `starting_sha`.
"""
