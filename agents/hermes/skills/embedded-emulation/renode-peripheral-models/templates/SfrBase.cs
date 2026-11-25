//
// Base of an MCU's SFR blocks: 16-bit registers with byte access merged by the model, and
// peripheral module disable. Rename the namespace/class prefix for your MCU family.
// This file is licensed under the MIT License.
//
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.CPU;

namespace Antmicro.Renode.Peripherals.MyFamily
{
    public abstract class MyFamily_Sfr : IWordPeripheral, IBytePeripheral, IKnownSize
    {
        protected MyFamily_Sfr(IMachine machine, long size)
        {
            this.machine = machine;
            Size = size;
        }

        public long Size { get; }

        /// <summary>Module disable: while Pmd says bit PmdBit is set, the module reads 0 and ignores writes.</summary>
        public MyFamily_SystemControl Pmd { get; set; }
        public int PmdBit { get; set; } = -1;

        public ushort ReadWord(long offset) => Disabled ? (ushort)0 : Read(offset & ~1L);
        public void WriteWord(long offset, ushort value) { if(!Disabled) { Write(offset & ~1L, value, 0xFFFF); } }
        public byte ReadByte(long offset) => Disabled ? (byte)0 : (byte)(Read(offset & ~1L) >> (int)(8 * (offset & 1)));
        public void WriteByte(long offset, byte value)
        {
            if(Disabled)
            {
                return;
            }
            var shift = (int)(8 * (offset & 1));
            Write(offset & ~1L, (ushort)(value << shift), (ushort)(0xFF << shift));
        }

        public abstract void Reset();

        /// <summary>Read the word at even offset (side effects allowed: it is a real read).</summary>
        protected abstract ushort Read(long offset);
        /// <summary>Write only the bits of mask (0xFFFF, 0x00FF or 0xFF00).</summary>
        protected abstract void Write(long offset, ushort value, ushort mask);

        protected static ushort Merge(ushort old, ushort value, ushort mask) => (ushort)((old & ~mask) | (value & mask));
        protected static bool Bit(ushort value, int bit) => (value & (1 << bit)) != 0;
        protected bool Disabled => Pmd != null && PmdBit >= 0 && Pmd.ModuleDisabled(PmdBit);

        /// <summary>Bring virtual time up to the executing instruction (CPU thread only).</summary>
        internal static void SyncTime(IMachine machine)
        {
            if(machine.SystemBus.TryGetCurrentCPU(out var cpu) && cpu is BaseCPU b && b.OnPossessedThread)
            {
                b.SyncTime();
            }
        }

        protected readonly IMachine machine;
    }
}
