// Copyright (c) 2021 The Bitcoin Developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#ifndef BITCOIN_CURRENCYUNIT_H
#define BITCOIN_CURRENCYUNIT_H

class ArgsManager;

constexpr bool DEFAULT_ECASH = false;
constexpr bool DEFAULT_XEC = false;

void SetupCurrencyUnitOptions(ArgsManager &argsman);

#endif // BITCOIN_CURRENCYUNIT_H
