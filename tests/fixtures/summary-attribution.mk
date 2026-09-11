# Its own summary.
described:
	first command
	second command \
	  with a continuation

undescribed:
	third command

# Past a .PHONY and a variable.
.PHONY: separated
SEPARATED_ROOT ?= /somewhere
separated:
	fourth command
