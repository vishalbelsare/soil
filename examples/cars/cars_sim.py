"""
This is an example of a simplified city, where there are Passengers and Drivers that can take those passengers
from their location to their desired location.

An example scenario could play like the following:

- Drivers start in the `wandering` state, where they wander around the city until they have been assigned a journey
- Passenger(1) tells every driver that it wants to request a Journey.
- Each driver receives the request.
  If Driver(2) is interested in providing the Journey, it asks Passenger(1) to confirm that it accepts Driver(2)'s request
- When Passenger(1) accepts the request, two things happen:
    - Passenger(1) changes its state to `driving_home`
    - Driver(2) starts moving towards the origin of the Journey
- Once Driver(2) reaches the origin, it starts moving itself and Passenger(1) to the destination of the Journey
- When Driver(2) reaches the destination (carrying Passenger(1) along):
    - Driver(2) starts wondering again
    - Passenger(1) dies, and is removed from the simulation
- If there are no more passengers available in the simulation, Drivers die
"""
from __future__ import annotations
from typing import Optional
from soil import *
from soil import events
from mesa.space import MultiGrid


# More complex scenarios may use more than one type of message  between objects.
# A common pattern is to use `enum.Enum` to represent state changes in a request.
@dataclass
class Journey:
    """
    This represents a request for a journey. Passengers and drivers exchange this object.

    A journey may have a driver assigned or not. If the driver has not been assigned, this
    object is considered a "request for a journey".
    """

    origin: (int, int)
    destination: (int, int)
    tip: float

    passenger: Passenger
    driver: Optional[Driver] = None


class City(EventedEnvironment):
    """
    An environment with a grid where drivers and passengers will be placed.

    The number of drivers and riders is configurable through its parameters:

    :param str n_cars: The total number of drivers to add
    :param str n_passengers: The number of passengers in the simulation
    :param list agents: Specific agents to use in the simulation. It overrides the `n_passengers`
    and `n_cars` params.
    :param int height: Height of the internal grid
    :param int width: Width of the internal grid
    """
    n_cars = 1
    n_passengers = 10
    height = 100
    width = 100

    def init(self):
        self.grid = MultiGrid(width=self.width, height=self.height, torus=False)
        if not self.get_agents():
            self.add_agents(Driver, k=self.n_cars)
            self.add_agents(Passenger, k=self.n_passengers)

        for agent in self.get_agents():
            self.grid.place_agent(agent, (0, 0))
            self.grid.move_to_empty(agent)
        
        self.total_earnings = 0
        self.add_model_reporter("total_earnings")

    @report
    @property
    def number_passengers(self):
        return self.count_agents(agent_class=Passenger)


class Driver(Evented, FSM):
    pos = None
    journey = None
    earnings = 0

    def on_receive(self, msg, sender):
        """This is not a state. It will run (and block) every time process_messages is invoked"""
        if self.journey is None and isinstance(msg, Journey) and msg.driver is None:
            msg.driver = self
            self.journey = msg

    def check_passengers(self):
        """If there are no more passengers, stop forever"""
        c = self.count_agents(agent_class=Passenger)
        self.debug(f"Passengers left {c}")
        return c

    @state(default=True)
    async def wandering(self):
        """Move around the city until a journey is accepted"""
        target = None
        if not self.check_passengers():
            return self.die("No passengers left")
        self.journey = None
        while self.journey is None:  # No potential journeys detected (see on_receive)
            if target is None or not self.move_towards(target):
                target = self.random.choice(
                    self.model.grid.get_neighborhood(self.pos, moore=False)
                )

            if not self.check_passengers():
                return self.die("No passengers left")
            # This will call on_receive behind the scenes, and the agent's status will be updated
            self.process_messages()
            await self.delay(30)  # Wait at least 30 seconds before checking again

        try:
            # Re-send the journey to the passenger, to confirm that we have been selected
            self.journey = await self.journey.passenger.ask(self.journey, timeout=60, delay=5)
        except events.TimedOut:
            # No journey has been accepted. Try again
            self.journey = None
            return

        return self.driving

    @state
    async def driving(self):
        """The journey has been accepted. Pick them up and take them to their destination"""
        self.info(f"Driving towards Passenger {self.journey.passenger.unique_id}")
        while self.move_towards(self.journey.origin):
            await self.delay()
        self.info(f"Driving {self.journey.passenger.unique_id} from {self.journey.origin} to {self.journey.destination}")
        while self.move_towards(self.journey.destination, with_passenger=True):
            await self.delay()
        self.info("Arrived at destination")
        self.earnings += self.journey.tip
        self.model.total_earnings += self.journey.tip
        if not self.check_passengers():
            return self.die("No passengers left")
        return self.wandering

    def move_towards(self, target, with_passenger=False):
        """Move one cell at a time towards a target"""
        self.debug(f"Moving { self.pos } -> { target }")
        if target[0] == self.pos[0] and target[1] == self.pos[1]:
            return False

        next_pos = [self.pos[0], self.pos[1]]
        for idx in [0, 1]:
            if self.pos[idx] < target[idx]:
                next_pos[idx] += 1
                break
            if self.pos[idx] > target[idx]:
                next_pos[idx] -= 1
                break
        self.model.grid.move_agent(self, tuple(next_pos))
        if with_passenger:
            self.journey.passenger.pos = (
                self.pos
            )  # This could be communicated through messages
        return True


class Passenger(Evented, FSM):
    pos = None

    def on_receive(self, msg, sender):
        """This is not a state. It will be run synchronously every time `process_messages` is run"""

        if isinstance(msg, Journey):
            self.journey = msg
            return msg

    @default_state
    @state
    async def asking(self):
        destination = (
            self.random.randint(0, self.model.grid.height-1),
            self.random.randint(0, self.model.grid.width-1),
        )
        self.journey = None
        journey = Journey(
            origin=self.pos,
            destination=destination,
            tip=self.random.randint(10, 100),
            passenger=self,
        )

        timeout = 60
        expiration = self.now + timeout
        self.info(f"Asking for journey at: { self.pos }")
        self.model.broadcast(journey, ttl=timeout, sender=self, agent_class=Driver)
        while not self.journey:
            self.debug(f"Waiting for responses at: { self.pos }")
            try:
                # This will call process_messages behind the scenes, and the agent's status will be updated
                # If you want to avoid that, you can call it with: check=False
                await self.received(expiration=expiration, delay=10)
            except events.TimedOut:
                self.info(f"Still no response. Waiting at: { self.pos }")
                self.model.broadcast(
                    journey, ttl=timeout, sender=self, agent_class=Driver
                )
                expiration = self.now + timeout
        self.info(f"Got a response! Waiting for driver")
        return self.driving_home

    @state
    async def driving_home(self):
        while (
            self.pos[0] != self.journey.destination[0]
            or self.pos[1] != self.journey.destination[1]
        ):
            try:
                await self.received(timeout=60)
            except events.TimedOut:
                pass

        self.die("Got home safe!")


simulation = Simulation(name="RideHailing",
                        model=City,
                        seed="carsSeed",
                        max_time=1000,
                        parameters=dict(n_passengers=2))

if __name__ == "__main__":
    easy(simulation)
