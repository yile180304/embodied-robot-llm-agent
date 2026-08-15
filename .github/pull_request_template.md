## Summary

Describe the user-visible or engineering change and why it is needed.

## Validation

- [ ] Python tests pass
- [ ] Web tests pass when frontend code changes
- [ ] Web production build passes when frontend code changes
- [ ] Real local MQTT Broker was used when claiming MQTT integration evidence
- [ ] Failure and safety paths were checked

## Boundaries

- [ ] No secrets, `.env`, local build outputs, or personal data are included
- [ ] Model output is not executed as code
- [ ] Tool Registry, Schema, Safety, and `(task_id, seq)` idempotency remain intact
- [ ] Simulation/localhost/compile evidence is not described as real hardware evidence
- [ ] New third-party code or assets include source and license information

## Notes

List known limitations, skipped checks, screenshots, transcripts, or follow-up work.
