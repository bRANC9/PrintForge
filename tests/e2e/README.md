# e2e tests

End-to-end tests for the full `prompt -> OpenSCAD -> STL -> preview` pipeline
land here in Phase 2/4. The Ollama call is mocked so the tests stay
deterministic; the OpenSCAD call runs against the sandbox image.
