from . import SimData
from ...relocation import Relocation

class PointTo(SimData):
    pointto_name: str = NotImplemented
    pointto_type: int = NotImplemented
    type = SimData.TYPE_OBJECT
    addend: int = 0

    @staticmethod
    def static_size(arch):
        return arch.bytes

    def value(self):
        return bytes(self.size)

    def relocations(self):
        return [SimpleRelocation(self.owner, self.owner.make_import(self.pointto_name, self.pointto_type), self.relative_addr, self.addend)]


class SimpleRelocation(Relocation):
    def __init__(self, owner, symbol, addr, addend):
        super(SimpleRelocation, self).__init__(owner, symbol, addr)
        self.addend = addend

    @property
    def value(self):
        return self.resolvedby.rebased_addr + self.addend