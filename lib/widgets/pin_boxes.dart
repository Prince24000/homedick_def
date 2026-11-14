import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

/// The PIN, as six boxes (the owner's spec, 2026-08-26): "since theres a
/// limit to the pin to be created, let it be six pin as must so the input
/// pin should just be six boxes" — and only the NUMBER keyboard ever
/// appears.
///
/// One real (invisible) TextField carries the input; the boxes are its
/// visual cells. Auto-submits the moment the sixth digit lands — no Enter,
/// no button — and clears itself right after, so a wrong-PIN retry starts
/// from empty boxes. Digits show as dots unless [revealed].
class PinBoxes extends StatefulWidget {
  const PinBoxes({
    super.key,
    this.length = 6,
    required this.onComplete,
    required this.fillColor,
    required this.borderColor,
    required this.focusColor,
    required this.textColor,
    this.revealed = false,
  });

  final int length;
  final void Function(String pin) onComplete;
  final Color fillColor;
  final Color borderColor;
  final Color focusColor;
  final Color textColor;
  final bool revealed;

  @override
  State<PinBoxes> createState() => _PinBoxesState();
}

class _PinBoxesState extends State<PinBoxes> {
  final _ctl = TextEditingController();
  final _focus = FocusNode();

  @override
  void dispose() {
    _ctl.dispose();
    _focus.dispose();
    super.dispose();
  }

  void _changed(String v) {
    setState(() {});
    if (v.length == widget.length) {
      final pin = v;
      // Cleared BEFORE the callback returns a new question: a wrong PIN
      // re-asks, and the retry must meet empty boxes, not the old digits.
      _ctl.clear();
      widget.onComplete(pin);
    }
  }

  @override
  Widget build(BuildContext context) {
    final typed = _ctl.text;
    return Stack(
      children: [
        // The real input: invisible, but focused and fed by the NUMBER
        // keyboard only. Offstage would drop the connection to the IME;
        // zero-opacity keeps it.
        Opacity(
          opacity: 0,
          // Opacity 0 DROPS its child's semantics by default, which left a
          // screen reader with no field to find at all: the boxes below are
          // decoration, and this invisible field is the only real input.
          alwaysIncludeSemantics: true,
          child: SizedBox(
            height: 1,
            child: TextField(
              controller: _ctl,
              focusNode: _focus,
              autofocus: true,
              decoration: InputDecoration(
                labelText: 'PIN, ${widget.length} digits',
              ),
              // A PASSWORD FIELD underneath, even though nothing of it is
              // drawn: this is what stops a screen reader announcing each
              // digit and the keyboard remembering them.
              obscureText: true,
              autocorrect: false,
              enableSuggestions: false,
              keyboardType: TextInputType.number,
              inputFormatters: [
                FilteringTextInputFormatter.digitsOnly,
                LengthLimitingTextInputFormatter(widget.length),
              ],
              onChanged: _changed,
            ),
          ),
        ),
        GestureDetector(
          behavior: HitTestBehavior.opaque,
          onTap: () => _focus.requestFocus(),
          // The dots and digits are drawn, not announced: the field above
          // is what a screen reader reads (as a password field), so the
          // cells must not read the digits out one by one when revealed.
          child: ExcludeSemantics(
            child: Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              for (var i = 0; i < widget.length; i++) ...[
                if (i > 0) const SizedBox(width: 8),
                Container(
                  width: 44,
                  height: 52,
                  alignment: Alignment.center,
                  decoration: BoxDecoration(
                    color: widget.fillColor,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(
                      // The NEXT box to fill wears the accent, so the eye
                      // knows where it is without a cursor.
                      color: i == typed.length
                          ? widget.focusColor
                          : widget.borderColor,
                      width: i == typed.length ? 1.6 : 1,
                    ),
                  ),
                  child: Text(
                    i < typed.length
                        ? (widget.revealed ? typed[i] : '•')
                        : '',
                    style: TextStyle(
                      color: widget.textColor,
                      fontSize: widget.revealed ? 20 : 26,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
              ],
            ],
          ),
          ),
        ),
      ],
    );
  }
}