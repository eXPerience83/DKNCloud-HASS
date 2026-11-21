# DKN Cloud Sleep LAB live evidence

The trials used one authorized device in ventilation mode (P2=3), low fan
speed, and a five-minute Sleep duration confirmed by GET before each trial.
The NAS runner used the integration's Pusher client with one private
subscription, INFO every 10 seconds, scoped GET and separate physical
observations. Each trial used its own authorization. Times below are relative
to the server acknowledgment of the Sleep selection. PUSH and GET times
describe receipt and may lag the physical transition.

| Trial | Intervention | Observed result |
|---|---|---|
| A | Start OFF, select Sleep, send one remote P1 ON at t+240.08 s | Physical ON corroborated. GET at t+300.09 still reported ON. First OFF PUSH at t+311.10, scene Sleep. Operator confirmed physical OFF. |
| C | Start ON, select Sleep, send one remote P1 OFF at t+120.06 s | First OFF PUSH at t+129.32. Operator confirmed physical OFF. GET at t+390.08 still reported OFF and scene Sleep. |
| B, revised | Start ON, select Sleep, prompt for panel OFF at t+30.00, then send one remote P1 ON at t+240.00 after checking OFF and retained Sleep | First panel OFF PUSH at t+53.13. Pre-wake GET at t+220.09 confirmed OFF and Sleep. First re-wake ON PUSH at t+248.81. GET at t+300.08 still showed ON. First subsequent OFF PUSH at t+312.61, scene Sleep. GET and physical observation corroborated OFF. |

A and B support countdown from Sleep selection in the observed conditions.
Their OFF reports arrived roughly one minute after remote ON, rather than
five minutes after it. In revised B, a panel OFF and subsequent remote ON
before expiry did not prevent shutdown near the original deadline. C shows
that an earlier remote OFF did not clear the reported Sleep scene through
t+390.08. Retention of the scene does not establish whether C's timer was
internally cancelled.

A closed at t+371.82 and revised B at t+360.65 after corroboration, earlier
than the planned t+600 limit. C completed its full observation. The runner
sent no occupied command within any measured window. After each trial, GET
verified restoration to OFF, occupied and a 30-minute Sleep duration.

An initial A attempt stopped after updating the duration and before setting
the scene or sending P1. The duration was restored before starting A again.
It contributed no timer evidence.

## Open question

F will test a wake after the timer has already caused a natural OFF, with
the original Sleep scene left in place by the LAB. F requires exact readback
of a one-minute duration before starting. Its result is pending. These
observations do not change the Home Assistant integration's Sleep policy.
