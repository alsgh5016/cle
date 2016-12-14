# -*-coding:utf8 -*-
# This file is part of Mach-O Loader for CLE.
# Contributed December 2016 by Fraunhofer SIT (https://www.sit.fraunhofer.de/en/).

from .symbol import SYMBOL_TYPE_SECT
from .. import Backend
from ...errors import CLEInvalidBinaryError, CLECompatibilityError, CLEOperationError
import logging, struct, sys, archinfo, cStringIO
from os import SEEK_CUR, SEEK_SET
from .section import MachOSection
from .symbol import MachOSymbol
from .segment import MachOSegment
from .binding import BindingHelper, read_uleb

l = logging.getLogger('cle.MachO.main')

__all__ = ('MachO', 'MachOSection', 'MachOSegment',)


class MachO(Backend):
    """
    Mach-O binaries for CLE

    The Mach-O format is notably different from other formats, as such:
    *   Sections are always part of a segment, self.sections will thus be empty
    *   Symbols cannot be categorized like in ELF
    *   Symbol resolution must be handled by the binary
    *   Rebasing cannot be done statically (i.e. self.rebase_addr is ignored for now)
    *   ...
    """
    MH_MAGIC_64 = 0xfeedfacf
    MH_CIGAM_64 = 0xcffaedfe
    MH_MAGIC = 0xfeedface
    MH_CIGAM = 0xcefaedfe

    supported_filetypes = ['mach-o']

    def __init__(self, binary, **kwargs):
        """
        :param skip_extended_parsing: If present and True skips everything except minimal load-command parsing
        """
        super(MachO, self).__init__(binary, **kwargs)
        self.rebase_addr = 0  # required for some angr stuffs even though not supported

        self.struct_byteorder = None  # holds byteorder for struct.unpack(...)
        self.cputype = None
        self.cpusubtype = None
        self.filetype = None
        self.pie = None  # position independent executable?
        self.ncmds = None  # number of load commands
        self.flags = None  # binary flags
        self.sizeofcmds = None  # total size of load commands
        self.imported_libraries = ["Self"]  # ordinal 0 = SELF_LIBRARY_ORDINAL
        self.exports_by_name = {}  # note exports is currently a raw and unprocessed datastructure.
        # If we intend to use it we must first upgrade it to a class or somesuch
        self.entryoff = None
        self.unixthread_pc = None
        self.os = "MacOSX_based"
        self.lc_data_in_code = []  # data from LC_DATA_IN_CODE (if encountered). Format: (offset,length,kind)
        self.mod_init_func_pointers = []  # may be TUMB interworking
        self.mod_term_func_pointers = []  # may be THUMB interworking
        self.export_blob = None  # exports trie
        self.symbols = []  # array of symbols
        self.binding_blob = None  # binding information
        self.lazy_binding_blob = None  # lazy binding information
        self.weak_binding_blob = None  # weak binidng information

        # Begin parsing the file
        try:

            binary_file = self.binary_stream
            # get magic value and determine endianness
            self.struct_byteorder = self._detect_byteorder(struct.unpack("=I", binary_file.read(4))[0])

            # parse the mach header:
            # (ignore all irrelevant fiels)
            self._parse_mach_header(binary_file)

            # determine architecture
            arch_ident = self._detect_arch_ident()
            if not arch_ident:
                raise CLECompatibilityError(
                    "Unsupported architecture: 0x{0:X}:0x{1:X}".format(self.cputype, self.cpusubtype))

            # Create archinfo
            # Note that this should be customized for Apple ABI (TODO)
            self.set_arch(
                archinfo.arch_from_id(arch_ident, endness="lsb" if self.struct_byteorder == "<" else "msb"))

            # Start reading load commands
            lc_offset = (7 if self.arch.bits == 32 else 8) * 4

            # Possible optimization: Remove all unecessary calls to seek()
            # Load commands have a common structure: First 4 bytes identify the command by a magic number
            # second 4 bytes determine the commands size. Everything after this generic "header" is command-specific
            # this makes parsing the commands easy.
            # The documentation for Mach-O is at http://opensource.apple.com//source/xnu/xnu-1228.9.59/EXTERNAL_HEADERS/mach-o/loader.h
            count = 0
            offset = lc_offset
            while count < self.ncmds and (offset - lc_offset) < self.sizeofcmds:
                count += 1
                (cmd, size) = self._unpack("II", binary_file, offset, 8)

                # check for segments that interest us
                if cmd in [0x1, 0x19]:  # LC_SEGMENT,LC_SEGMENT_64
                    l.debug("Found LC_SEGMENT(_64) @ 0x{0:X}".format(offset))
                    self._load_segment(binary_file, offset)
                elif cmd == 0x2:  # LC_SYMTAB
                    l.debug("Found LC_SYMTAB @ 0x{0:X}".format(offset))
                    self._load_symtab(binary_file, offset)
                elif cmd in [0x22, 0x80000022]:  # LC_DYLD_INFO(_ONLY)
                    l.debug("Found LC_DYLD_INFO(_ONLY) @ 0x{0:X}".format(offset))
                    self._load_dyld_info(binary_file, offset)
                elif cmd in [0xc, 0x8000001c, 0x80000018]:  # LC_LOAD_DYLIB, LC_REEXPORT_DYLIB,LC_LOAD_WEAK_DYLIB
                    l.debug("Found LC_*_DYLIB @ 0x{0:X}".format(offset))
                    self._load_dylib_info(binary_file, offset)
                elif cmd == 0x80000028:  # LC_MAIN
                    l.debug("Found LC_MAIN @ 0x{0:X}".format(offset))
                    self._load_lc_main(binary_file, offset)
                elif cmd == 0x5:  # LC_UNIXTHREAD
                    l.debug("Found LC_UNIXTHREAD @ 0x{0:X}".format(offset))
                    self._load_lc_unixthread(binary_file, offset)
                elif cmd == 0x26:  # LC_FUNCTION_STARTS
                    l.debug("Found LC_FUNCTION_STARTS @ 0x{0:X}".format(offset))
                    self._load_lc_function_starts(binary_file, offset)
                elif cmd == 0x29:  # LC_DATA_IN_CODE
                    l.debug("Found LC_DATA_IN_CODE @ 0x{0:X}".format(offset))
                    self._load_lc_data_in_code(binary_file, offset)
                elif cmd in [0x21, 0x2c]:  # LC_ENCRYPTION_INFO(_64)
                    l.debug("Found LC_ENCRYPTION_INFO @ 0x{0:X}".format(offset))
                    self._assert_unencrypted(binary_file, offset)

                # update bookkeeping
                offset += size

            # Assertion to catch malformed binaries - YES this is needed!
            if count < self.ncmds or (offset - lc_offset) < self.sizeofcmds:
                raise CLEInvalidBinaryError(
                    "Assertion triggered: {0} < {1} or {2} < {3}".format(count, self.ncmds, (offset - lc_offset),
                                                                         self.sizeofcmds))
        except IOError as e:
            l.exception(e)
            raise CLEOperationError(e)

        # File is read, begin populating internal fields
        self._resolve_entry()
        self._resolve_symbols()
        self._parse_mod_funcs()

    def is_thumb_interworking(s, address):
        """Returns true if the given address is a THUMB interworking address"""
        # Note: Untested
        return (not s.is_64bit) and address & 1

    def decode_thumb_interworking(self, address):
        """Decodes a thumb interworking address"""
        # Note: Untested
        return address & 0xFFFFFFFE if self.is_thumb_interworking(address) else address

    def _parse_mod_funcs(self):
        l.debug("Parsing module init/term function pointers")

        fmt = "Q" if self.is_64bit else "I"
        size = 8 if self.is_64bit else 4

        # factoring out common code
        def parse_mod_funcs_internal(s, target):
            for i in range(s.vaddr, s.vaddr + s.memsize, size):
                addr = self._unpack_with_byteorder(fmt, "".join(self.memory.read_bytes(i, size)))[0]
                l.debug("Addr: 0x{0:X}".format(addr))
                target.append(addr)

        for seg in self.segments:
            for sec in seg.sections:

                if sec.type == 0x9:  # S_MOD_INIT_FUNC_POINTERS
                    l.debug("Section {0} contains init pointers".format(sec.sectname))
                    parse_mod_funcs_internal(sec, self.mod_init_func_pointers)
                elif sec.type == 0xa:  # S_MOD_TERM_FUNC_POINTERS
                    l.debug("Section {0} contains term pointers".format(sec.sectname))
                    parse_mod_funcs_internal(sec, self.mod_term_func_pointers)

        l.debug("Done parsing module init/term function pointers")

    def find_segment_by_name(self, name):
        for s in self.segments:
            if s.segname == name:
                return s
        return None

    def _resolve_entry(self):
        if self.entryoff:
            self._entry = self.find_segment_by_name("__TEXT").vaddr + self.entryoff
        elif self.unixthread_pc:
            self._entry = self.unixthread_pc
        else:
            l.warning("No entry point found")
            self._entry = 0

    def _read(self, file, offset, size):
        """
        Simple read abstraction, reads size bytes from offset in file
        :param offset: Offset to seek() to
        :param size: number of bytes to be read
        :return: string of bytes or "" for EOF
        """
        file.seek(offset)
        return file.read(size)

    def _unpack_with_byteorder(self, fmt, input):
        """
        Appends self.struct_byteorder before fmt to ensure usage of correct byteorder
        :return: struct.unpack(self.struct_byteorder+fmt,input)
        """
        return struct.unpack(self.struct_byteorder + fmt, input)

    def _unpack(self, fmt, file, offset, size):
        """Convenience"""
        return self._unpack_with_byteorder(fmt, self._read(file, offset, size))

    def _parse_mach_header(self, f):
        """
        Parses the mach-o header and sets
        self.cputype, self.cpusubtype, self.pie, self.ncmds,self.flags,self.sizeofcmds and self.filetype

        Currently ignores any type of information that is not directly relevant to analyses
        :param f: The binary as a file object
        :return: None
        """
        # this method currently disregards any differences between 32 and 64 bit code
        (_, self.cputype, self.cpusubtype, self.filetype, self.ncmds, self.sizeofcmds,
         self.flags) = self._unpack("7I", f, 0, 28)

        self.pie = bool(self.flags & 0x200000)  # MH_PIE

        if not bool(self.flags & 0x80):  # ensure MH_TWOLEVEL
            raise CLEInvalidBinaryError("Cannot handle non MH_TWOLEVEL binaries")

    def _detect_byteorder(self, magic):
        """Determines the binary's byteorder """

        l.debug("Magic is 0x{0:X}".format(magic))

        host_is_little = sys.byteorder == 'little'

        if host_is_little:
            if magic in [MachO.MH_MAGIC_64, MachO.MH_MAGIC]:
                l.debug("Detected little-endian")
                return "<"
            elif magic in [MachO.MH_CIGAM, MachO.MH_CIGAM_64]:
                l.debug("Detected big-endian")
                return ">"
            else:
                l.debug("Not a mach-o file")
                raise CLECompatibilityError()
        else:
            if magic in [MachO.MH_MAGIC_64, MachO.MH_MAGIC]:
                l.debug("Detected big-endian")
                return ">"
            elif magic in [MachO.MH_CIGAM_64, MachO.MH_CIGAM]:
                l.debug("Detected little-endian")
                return "<"
            else:
                l.debug("Not a mach-o file")
                raise CLECompatibilityError()

    def _resolve_symbols(self):
        """This method resolves all symbols and sets their missing attributes"""
        # TODO: Should attempt to fill self.imports in accordance with angr's expectations
        # note: no rebasing support

        # trigger parsing of exports
        self._parse_exports(self.export_blob)

        # Set up search tables for searching
        section_tab = [None]  # first section is NO_SECT
        for seg in self.segments:
            section_tab.extend(seg.sections)

        # A new memory area is created to hold external (undefined) symbols
        ext_symbol_start = 0xff00000000000000 if self.arch.bits == 64 else 0xff000000
        ext_symbol_end = ext_symbol_start

        # Update symbol properties
        for sym in self.symbols:

            sym._is_export = sym.name in self.exports_by_name

            if sym.is_stab:  # stab symbols are debugging information - don't care!
                l.debug("Symbol '{0}' is debugging information, skipping".format(
                    sym.name))
                continue

            if sym.name in self.exports_by_name:
                l.debug("Symbol '{0}' is an export".format(sym.name))
                sym.is_export = True
            else:
                l.debug("Symbol '{0}' is not an export".format(sym.name))
                sym.is_export = False

            if sym.is_common:
                l.debug("Symbol '{0}' is common, updating size".format(sym.name))
                sym.size = sym.n_value

            if sym.sym_type == SYMBOL_TYPE_SECT:
                l.debug("Symbol '{0}' is N_SECT, updating names and address".format(sym.name))
                sec = section_tab[sym.n_sect]
                if sec is not None:
                    sym.segment_name = sec.segname
                    sym.section_name = sec.sectname

                sym.addr = sym.n_value

            elif sym.is_import():
                l.debug("Symbol '{0}' is imported!".format(sym.name))
                sym.library_name = self.imported_libraries[sym.library_ordinal]

            # if the symbol has no address we assign one from the special memory area
            if sym.addr is None:
                sym.addr = ext_symbol_end
                ext_symbol_end += sym.size
                l.debug("Assigning address 0x{0:X} to symbol '{1}'".format(sym.addr, sym.name))

        # Add special memory area for symbols:
        self.memory.add_backer(ext_symbol_start, "\x00" * (ext_symbol_end - ext_symbol_start))

        # Perform binding
        bh = BindingHelper(self)  # TODO: Make this configurable
        bh.do_normal_bind(self.binding_blob)
        bh.do_lazy_bind(self.lazy_binding_blob)
        if self.weak_binding_blob is not None and len(self.weak_binding_blob) > 0:
            l.info("Found weak binding blob. According to current state of knowledge, weak binding "
                   "is only sensible if multiple binaries are involved and is thus skipped.")

        # Add to symbols_by_addr
        # All (resolvable) symbols should be resolved by now
        for sym in self.symbols:
            if not sym.is_stab:
                if sym.addr is not None:
                    self.symbols_by_addr[sym.addr] = sym
                else:
                    # todo: this might be worth an error
                    l.warn("Non-stab symbol '{0}' @ 0x{1:X} has no address.".format(sym.name, sym.symtab_offset))

    def _parse_exports(self, blob):
        """
        Parses the exports trie
        """
        l.debug("Parsing exports")

        if blob is None:
            l.debug("Parsing exports done: No exports found")
            return

        # Note some of these fields are currently not used, keep them in to make used variables explicit
        index = 0
        sym_str = ""
        # index,str
        nodes_to_do = [(0, "")]
        blob_f = cStringIO.StringIO(blob)  # easier to handle seeking here

        # constants
        FLAGS_KIND_MASK = 0x03
        FLAGS_KIND_REGULAR = 0x00
        FLAGS_KIND_THREAD_LOCAL = 0x01
        FLAGS_WEAK_DEFINITION = 0x04
        FLAGS_REEXPORT = 0x08
        FLAGS_STUB_AND_RESOLVER = 0x10

        try:
            while True:
                (index, sym_str) = nodes_to_do.pop()
                l.debug("Processing node 0x{0:X} '{1}'".format(index, sym_str))
                blob_f.seek(index, SEEK_SET)
                info_len = struct.unpack("B", blob_f.read(1))[0]
                if info_len > 127:
                    # special case
                    blob_f.seek(-1, SEEK_CUR)
                    tmp = read_uleb(blob, blob_f.tell())  # a bit kludgy
                    info_len = tmp[0]
                    blob_f.seek(tmp[1], SEEK_CUR)

                if info_len > 0:
                    # a symbol is complete
                    tmp = read_uleb(blob, blob_f.tell())
                    blob_f.seek(tmp[1], SEEK_CUR)
                    flags = tmp[0]
                    if flags & FLAGS_REEXPORT:
                        # REEXPORT: uleb:lib ordinal, zero-term str
                        tmp = read_uleb(blob, blob_f.tell())
                        blob_f.seek(tmp[1], SEEK_CUR)
                        lib_ordinal = tmp[0]
                        lib_sym_name = ""
                        char = blob_f.read(1)
                        while char != '\x00':
                            lib_sym_name += char
                            char = blob_f.read(1)
                        l.info("Found REEXPORT export '{0}': {1},'{2}'".format(sym_str, lib_ordinal, lib_sym_name))
                        self.exports_by_name[sym_str] = (flags, lib_ordinal, lib_sym_name)
                    elif flags & FLAGS_STUB_AND_RESOLVER:
                        # STUB_AND_RESOLVER: uleb: stub offset, uleb: resovler offset
                        l.warn("EXPORT: STUB_AND_RESOLVER found")
                        tmp = read_uleb(blob, blob_f.tell())
                        blob_f.seek(tmp[1], SEEK_CUR)
                        stub_offset = tmp[0]
                        tmp = read_uleb(blob, blob_f.tell())
                        blob_f.seek(tmp[1], SEEK_CUR)
                        resolver_offset = tmp[0]
                        l.info("Found STUB_AND_RESOLVER export '{0}': 0x{1:X},0x{2:X}'".format(sym_str, stub_offset,
                                                                                               resolver_offset))
                        self.exports_by_name[sym_str] = (flags, stub_offset, resolver_offset)
                    else:
                        # normal: offset from mach header
                        tmp = read_uleb(blob, blob_f.tell())
                        blob_f.seek(tmp[1], SEEK_CUR)
                        symbol_offset = tmp[0] + self.segments[1].vaddr
                        l.info("Found normal export '{0}': 0x{1:X}".format(sym_str, symbol_offset))
                        self.exports_by_name[sym_str] = (flags, symbol_offset)

                child_count = struct.unpack("B", blob_f.read(1))[0]
                for i in range(0, child_count):
                    child_str = sym_str
                    char = blob_f.read(1)
                    while char != '\x00':
                        child_str += char
                        char = blob_f.read(1)
                    tmp = read_uleb(blob, blob_f.tell())
                    blob_f.seek(tmp[1], SEEK_CUR)
                    next_node = tmp[0]
                    l.debug("{0}. child: (0x{1:X},'{2}')".format(i, next_node, child_str))
                    nodes_to_do.append((next_node, child_str))

        except IndexError:
            # List is empty we are done!
            l.debug("Done parsing exports")

    @property
    def is_64bit(self):
        return self.arch.bits == 64

    def _detect_arch_ident(self):
        """
        Determines the binary's architecture by inspecting cputype and cpusubtype.
        :return: archinfo.arch_from_id-compatible ident string
        """
        # determine architecture by major CPU type
        try:
            arch_lookup = {
            # contains all supported architectures. Note that apple deviates from standard ABI, see Apple docs
                0x100000c: "aarch",
                0xc: "arm",
                0x7: "x86",
                0x1000007: "x64",
            }
            return arch_lookup[self.cputype]  # subtype currently not needed
        except KeyError:
            return None

    def _load_lc_data_in_code(self, f, off):
        l.debug("Parsing data in code")

        (_, _, dataoff, datasize) = self._unpack("4I", f, off, 16)
        for i in range(dataoff, datasize, 8):
            blob = self._unpack("IHH", f, i, 8)
            self.lc_data_in_code.append(blob)

        l.debug("Done parsing data in code")

    def _assert_unencrypted(self, f, off):
        l.debug("Asserting unencrypted file")
        (_, _, _, _, cryptid) = self._unpack("5I", f, off, 20)
        if cryptid > 0:
            l.error("Cannot load encrypted files")
            raise CLEInvalidBinaryError()

    def _load_lc_function_starts(self, f, off):
        # note that the logic below is based on Apple's dyldinfo.cpp, no official docs seem to exist
        l.debug("Parsing function starts")
        (_, _, dataoff, datasize) = self._unpack("4I", f, off, 16)

        i = 0
        end = datasize
        blob = self._read(f, dataoff, datasize)
        self.lc_function_starts = []

        address = None
        for seg in self.segments:
            if seg.offset == 0 and seg.filesize != 0:
                address = seg.vaddr
                break

        if address is None:
            l.error("Could not determine base-address for function starts")
            raise CLEInvalidBinaryError()
        l.debug("Located base-address: 0x{0:X}".format(address))

        while i < end:
            uleb = read_uleb(blob, i)

            if blob[i] == "\x00":
                break  # list is 0 terminated

            address += uleb[0]

            self.lc_function_starts.append(address)
            l.debug("Function start @ 0x{0:X}".format(uleb[0]))
            i += uleb[1]
        l.debug("Done parsing function starts")

    def _load_lc_main(self, f, offset):
        if self.entryoff is not None or self.unixthread_pc is not None:
            l.error("More than one entry point for main detected, abort.")
            raise CLEInvalidBinaryError()

        (_, _, self.entryoff, _) = self._unpack("2I2Q", f, offset, 24)
        l.debug("LC_MAIN: entryoff=0x{0:X}".format(self.entryoff))

    def _load_lc_unixthread(self, f, offset):
        if self.entryoff is not None or self.unixthread_pc is not None:
            l.error("More than one entry point for main detected, abort.")
            raise CLEInvalidBinaryError()

        # parse basic structure
        (_, cmdsize, flavor, long_count) = self._unpack("4I", f, offset, 16)

        # we only support 4 different types of thread state atm
        # TODO: This is the place to add x86 and x86_64 thread states
        if flavor == 1 and not self.is_64bit:  # ARM_THREAD_STATE or ARM_UNIFIED_THREAD_STATE or ARM_THREAD_STATE32
            blob = self._unpack("16I", f, offset + 16, 64)  # parses only until __pc
        elif flavor == 1 and self.is_64bit or flavor == 6:  # ARM_THREAD_STATE or ARM_UNIFIED_THREAD_STATE or ARM_THREAD_STATE64
            blob = self._unpack("33Q", f, offset + 16, 264)  # parses only until __pc
        else:
            l.error("Unknown thread flavor: {0}".format(flavor))
            raise CLECompatibilityError()

        self.unixthread_pc = blob[-1]
        l.debug("LC_UNIXTHREAD: __pc=0x{0:X}".format(self.unixthread_pc))

    def _load_dylib_info(self, f, offset):
        (_, size, name_offset, _, _, _) = self._unpack("6I", f, offset, 24)
        lib_name = self.parse_lc_str(f, offset + name_offset)
        l.debug("Adding library '{0}'".format(lib_name))
        self.imported_libraries.append(lib_name)

    def _load_dyld_info(self, f, offset):
        """
        Extracts information blobs for rebasing, binding and export
        """
        (_, _, roff, rsize, boff, bsize, wboff, wbsize, lboff, lbsize, eoff, esize) = self._unpack("12I", f, offset, 48)

        # Extract data blobs
        self.rebase_blob = self._read(f, roff, rsize)
        self.binding_blob = self._read(f, boff, bsize)
        self.weak_binding_blob = self._read(f, wboff, wbsize)
        self.lazy_binding_blob = self._read(f, lboff, lbsize)
        self.export_blob = self._read(f, eoff, esize)

    def _load_symtab(self, f, offset):
        """
        Handles loading of the symbol table
        :param f: input file
        :param offset: offset to the LC_SYMTAB structure
        :return:
        """

        (_, _, symoff, nsyms, stroff, strsize) = self._unpack("6I", f, offset, 24)

        # load string table
        self.strtab = self._read(f, stroff, strsize)

        # parse the symbol entries and create (unresolved) MachOSymbols.
        if self.arch.bits == 64:
            packstr = "I2BHQ"
            structsize = 16
        else:
            packstr = "I2BhI"
            structsize = 12

        self.symbols = []  # we cannot yet fill symbols_by_addr
        for i in range(0, nsyms):
            offset = (i * structsize) + symoff
            (n_strx, n_type, n_sect, n_desc, n_value) = self._unpack(packstr, f, offset, structsize)
            l.debug(
                "Adding symbol # {0} @ {6:X}: {1},{2},{3},{4},{5}".format(i, n_strx, n_type, n_sect, n_desc, n_value,
                                                                          offset))
            self.symbols.append(
                MachOSymbol(self, self.get_string(n_strx) if n_strx != 0 else "", None, offset, n_type, n_sect, n_desc,
                            n_value))

    def get_string(self, start):
        """Loads a string from the string table"""
        end = start
        if end > len(self.strtab):
            raise ValueError()

        while end < len(self.strtab):
            if self.strtab[end] == chr(0):
                return self.strtab[start:end]
            end += 1
        return self.strtab[start:]

    def parse_lc_str(self, f, start, limit=None):
        """Parses a lc_str data structure"""
        tmp = self._unpack("c", f, start, 1)[0]
        s = ""
        ctr = 0
        while tmp != chr(0) and (limit is None or ctr < limit):
            s += tmp
            ctr += 1
            tmp = self._unpack("c", f, start + ctr, 1)[0]

        return s

    def _load_segment(self, f, offset):
        """
        Handles LC_SEGMENT(_64) commands
        :param f: input file
        :param offset: starting offset of the LC_SEGMENT command
        :return:
        """
        # determine if 64 or 32 bit segment
        is64 = self.arch.bits == 64
        if not is64:
            segment_s_size = 56
            (_, cmdsize, segname, vmaddr, vmsize, fileoff, filesize, maxprot, initprot, nsects, flags) = self._unpack(
                "2I16s8I", f, offset, segment_s_size)
        else:
            segment_s_size = 72
            (_, cmdsize, segname, vmaddr, vmsize, fileoff, filesize, maxprot, initprot, nsects, flags) = self._unpack(
                "2I16s4Q4I", f, offset, segment_s_size)

        # Cleanup segname
        segname = segname.replace("\x00", "")
        l.debug("Processing segment '{0}'".format(segname))

        # create segment
        seg = MachOSegment(fileoff, vmaddr, filesize, vmsize, segname, nsects, [], flags, initprot, maxprot)

        # Parse section datastructures
        if not is64:
            # 32 bit
            section_s_size = 68
            section_s_packstr = "16s16s9I"
        else:
            # 64 bit
            section_s_size = 80
            # The correct packstring is "16s16s2Q8I", however we use a different one that merges the last two reserved
            # fields (reserved2,reserved3) because it makes the parsing logic below easier
            section_s_packstr = "16s16s2Q6IQ"

        section_start = offset + segment_s_size
        for i in range(0, nsects):
            # Read section
            l.debug("Processing section # {0} in '{1}'".format(i + 1, segname))
            (section_sectname, section_segname, section_vaddr, section_vsize, section_foff, section_align,
             section_reloff,
             section_nreloc, section_flags, r1, r2) = \
                self._unpack(section_s_packstr, f, (i * section_s_size) + section_start, section_s_size)

            # Clean segname and sectname
            section_sectname = section_sectname.replace("\x00", "")
            section_segname = section_segname.replace("\x00", "")

            # Create section
            sec = MachOSection(section_foff, section_vaddr, section_vsize, section_vsize, section_segname,
                               section_sectname,
                               section_align, section_reloff, section_nreloc, section_flags, r1, r2)

            # Store section
            seg.sections.append(sec)

        if segname == "__PAGEZERO":
            # TODO: What we actually need at this point is some sort of smart on-demand string or memory
            # This should not cause trouble because accesses to __PAGEZERO are SUPPOSED to crash (segment has access set to no access)
            # This optimization is here as otherwise several GB worth of zeroes would clutter our memory
            l.info("Found PAGEZERO, skipping backer for memory conservation")
        else:
            # Append segment data to memory
            blob = self._read(f, seg.offset, seg.filesize)
            if seg.filesize < seg.memsize:
                blob += "\x00" * (seg.memsize - seg.filesize)  # padding

            self.memory.add_backer(seg.vaddr, blob)

        # Store segment
        self.segments.append(seg)

    def get_symbol_by_address_fuzzy(self, address):
        """Locates a symbol by checking the given address against sym.addr, sym.bind_xrefs and sym.symbol_stubs"""
        for sym in self.symbols:
            if address == sym.addr or address in sym.bind_xrefs or address in sym.symbol_stubs:
                return sym

    def get_symbol(self, name, include_stab=False, fuzzy=False):
        """Returns all symbols matching name. Note that especially when include_stab=True there may be multiple symbols
        with the same name, therefore this method always returns an array
        :param include_stab: Include debugging symbols NOT RECOMMENDED
        :param fuzzy: Replace exact match with "contains"-style match
        """
        result = []
        for sym in self.symbols:

            if sym.is_stab and not include_stab:
                continue

            if fuzzy:
                if name in sym.name:
                    result.append(sym)
            else:
                if name == sym.name:
                    result.append(sym)

        return result

    def get_segment_by_name(self, name):
        """
        Searches for a MachOSegment with the given name and returns it
        :param name: Name of the sought segment
        :return: MachOSegment or None
        """
        for seg in self.segments:
            if seg.segname == name:
                return seg

        return None

    def __getitem__(self, item):
        """
        Syntactic sugar for get_segment_by_name
        """
        return self.get_segment_by_name(item)
