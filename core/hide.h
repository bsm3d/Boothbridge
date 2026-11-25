// Boothbridge
// © Benoit Saint-Moulin - 2026
// Version du fichier : 1.0.0
// Fonction du fichier : Header that renames the exported symbols and obscures format constants in the native core binary.

//
// Not encryption: it makes a static read of the binary slower, nothing more. The results are
// the same, the tests compare every byte with the Python reference.

#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

// ---- exported names: the source keeps its readable names, the binary exports these
#define bb_version q0
#define bb_pdb_build q1
#define bb_pdb_write q2
#define bb_anlz_build q3
#define bb_fold q4
#define bb_bands q5

// ---- numbers: v is stored xored with MASK and put back through a volatile read, so that
// the compiler cannot fold it into the instruction
namespace hide {

static volatile uint32_t seed = 0x9E3779B1u;

}  // namespace hide

#define HIDE_MASK 0x5A17C3E1u
#define HIDE_NEXT (0x9E3779B1u ^ HIDE_MASK)
#define K32(v) (uint32_t(uint32_t(v) ^ HIDE_MASK) ^ (hide::seed ^ HIDE_NEXT))

// ---- strings: stored xored with a position dependent key, decoded when used
namespace hide {

template <size_t N>
struct Enc {
    char d[N];
    constexpr Enc(const char (&s)[N]) : d() {
        for (size_t i = 0; i < N; i++) d[i] = char(s[i] ^ char(0x5A + i * 7));
    }
};

template <size_t N>
std::string dec(const Enc<N>& e) {
    const volatile char* p = e.d;
    std::string out;
    for (size_t i = 0; i + 1 < N; i++) out.push_back(char(p[i] ^ char(0x5A + i * 7)));
    return out;
}

}  // namespace hide

#define HS(lit) ([]() -> std::string { static constexpr hide::Enc<sizeof(lit)> e(lit); return hide::dec(e); }())
