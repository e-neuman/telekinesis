from telekinesis import Broker, Telekinesis, Connection, Session, Channel
import random
import asyncio
import time
import pytest

pytestmark = pytest.mark.asyncio
random.seed(42)

@pytest.fixture
def event_loop(): # This avoids 'Task was destroyed but it is pending!' message
    yield asyncio.get_event_loop()

async def test_walkthrough():
    class FaultyBroker(Broker): # Telekinesis should survive broker errors
        async def handle_message(self, connection, message):
            if random.random() < 0.03:
                self.logger.error('Gotcha!!!')
                message = Exception('Random Fault Injection')
            await super().handle_message(connection, message)

    broker_0 = await FaultyBroker().serve(port=8777)
    
    conn_0 = await Connection(Session(), 'ws://localhost:8777')
    broker_0.endpoint = Telekinesis(lambda x: (lambda y: x+y), conn_0.session) \
                                   ._add_listener(Channel(conn_0.session, is_public=True))
    
    broker_1 = await FaultyBroker().serve(port=8778) # Telekinesis works with clusters of Brokers
    await broker_1.add_broker('ws://localhost:8777', True)

    await asyncio.sleep(0.1)
    conn_1 = await Connection(Session(), 'ws://localhost:8778')

    f = await Telekinesis(conn_1.endpoint, conn_1.session)
    g = await f('Hello, ') # Telekinesis objects that return Telekinesis objects are welcome

    assert 'Hello, World' == await g('World')

    long_message = 'a'*2**20

    assert 'Hello, '+ long_message == await g(long_message) # Telekinesis should handle big messages

    broker_2 = await FaultyBroker().serve(port=8779) # Yet another Broker!
    await broker_2.add_broker('ws://localhost:8777', True)
    await broker_2.add_broker('ws://localhost:8778')

    await asyncio.sleep(0.1)
    conn_2 = await Connection(Session(), 'ws://localhost:8778')

    with pytest.raises(asyncio.TimeoutError):
        g_2 = await asyncio.wait_for(Telekinesis(g._target, conn_2.session)._execute(), 2)
    
    delegator_route = Telekinesis(lambda: g, conn_1.session)._delegate(conn_2.session.session_key.public_serial())

    g_2 = await Telekinesis(delegator_route, conn_2.session)._call()
    
    assert 'Hello, World!!' == await g_2('World!!')

    class Counter:
        def __init__(self):
            self.value = 0
        def increment(self):
            self.value += 1
            return self
        def to_be_masked(self):
            return 'May be sensitive'
        def _private(self):
            return 'Sensitive stuff'

    route_counter = Telekinesis(Counter, conn_0.session, ['to_be_masked'], max_delegation_depth=1) \
                               ._delegate(conn_1.session.session_key.public_serial()) # << Max delegation depth!

    counter = await Telekinesis(route_counter, conn_1.session)._call()

    assert await counter.increment().increment().value == 2

    with pytest.raises(Exception, match=r'.*not callable.*'):
        await counter.to_be_masked()
    
    with pytest.raises(Exception, match=r'.*Unauthorized.*'):
        c = counter.increment()
        c._state.pipeline[0] = ('get', 'to_be_masked')
        await c

    with pytest.raises(Exception, match=r'.*Unauthorized.*'):
        c = counter.increment()
        c._state.pipeline[0] = ('get', '_private')
        await c

    # Try to delegate
    counter_delegator_route = Telekinesis(lambda: counter, conn_1.session)\
                                         ._delegate(conn_2.session.session_key.public_serial())

    counter_2 = await Telekinesis(counter_delegator_route, conn_2.session)._call()

    with pytest.raises(asyncio.TimeoutError): # Max delegation depth doesn't allow it!
        await asyncio.wait_for(counter_2.value._execute(), 2)