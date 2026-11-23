import asyncio

from bitsai.matrix.throttle import EditThrottle


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


async def test_immediate_then_coalesced():
    sent, clock = [], Clock()

    async def send(c):
        sent.append(c)

    th = EditThrottle(send, min_interval=0.05, clock=clock)
    await th.update("a")
    assert sent == ["a"]
    await th.update("b")
    await th.update("c")
    assert sent == ["a"]
    await asyncio.sleep(0.15)
    assert sent == ["a", "c"]
    await th.aclose()


async def test_final_cancels_pending():
    sent, clock = [], Clock()

    async def send(c):
        sent.append(c)

    th = EditThrottle(send, min_interval=10, clock=clock)
    await th.update("a")
    await th.update("b")
    await th.final("z")
    assert sent == ["a", "z"]
    await asyncio.sleep(0.05)
    assert sent == ["a", "z"]
    await th.aclose()


async def test_sends_immediately_after_interval():
    sent, clock = [], Clock()

    async def send(c):
        sent.append(c)

    th = EditThrottle(send, min_interval=1.5, clock=clock)
    await th.update("a")
    clock.t += 2
    await th.update("b")
    assert sent == ["a", "b"]
    await th.aclose()


async def test_aclose_drops_pending():
    sent, clock = [], Clock()

    async def send(c):
        sent.append(c)

    th = EditThrottle(send, min_interval=0.05, clock=clock)
    await th.update("a")
    await th.update("b")
    await th.aclose()
    await asyncio.sleep(0.1)
    assert sent == ["a"]
